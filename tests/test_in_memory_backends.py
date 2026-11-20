"""The stl2 and db backends: the solids are triangulated in memory and every volume has to be closed."""
import re
import shutil
import subprocess
import sys
import textwrap

import numpy as np
import pytest

import CAD_to_OpenMC.assembly as ab
from CAD_to_OpenMC import occ_mesher
from CAD_to_OpenMC.h5m_io import read_h5m
from CAD_to_OpenMC.meshed_model import NO_VOLUME
from CAD_to_OpenMC.occ_mesher import MeshingError
from CAD_to_OpenMC.options import available_cores
from CAD_to_OpenMC.validate import MeshValidationError, check_model
from tests import models


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Run in an empty directory with a mesher_config that is restored afterwards."""
    monkeypatch.chdir(tmp_path)
    saved = dict(ab.mesher_config)
    yield ab.mesher_config
    ab.mesher_config.clear()
    ab.mesher_config.update(saved)


def imported(parts, merge=True):
    models.write_step(parts, "model.step")
    assembly = ab.Assembly(["model.step"], verbose=0)
    assembly.import_stp_files()
    if merge:
        assembly.merge_all()
    return assembly


def n_shared(model):
    return int(np.count_nonzero(model.surface_senses[:, 1] != NO_VOLUME))


def test_cladding_listed_before_its_pellets_is_closed(config):
    # with these tolerances the cladding used to get 304 open edges when it was meshed before the pellets
    config.update(tolerance=0.001, angular_tolerance=0.2)
    assembly = imported(models.pellet_stack(cladding_first=True))
    assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")

    model = read_h5m("model.h5m")
    assert model.volume_tags == ["clad", "fuel", "fuel", "fuel"]
    assert assembly.get_unique_tags() == {"clad", "fuel"}
    # the three pellets share their side with the cladding and their ends with each other
    assert n_shared(model) == 5
    assert check_model(model).ok
    assert assembly.validation_report.ok


@pytest.mark.parametrize("backend", ["stl2", "db"])
def test_parts_placed_by_an_assembly_are_closed(config, backend):
    assembly = imported(models.instanced_pin_lattice(2, 2))
    assembly.solids_to_h5m(backend=backend, h5m_filename="model.h5m")

    model = read_h5m("model.h5m")
    assert model.volume_tags == ["fuel", "clad", "water"] * 4
    # fuel-cladding and cladding-water in each of the 4 cells, and 4 walls between the cells
    assert n_shared(model) == 12
    assert check_model(model).ok


def test_threads_setting_reaches_the_mesher(config):
    assembly = imported(models.separate_boxes(2), merge=False)
    for threads, expected in ((3, 3), (None, available_cores())):
        config["threads"] = threads
        assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")
        assert assembly.meshed_model.info["threads"] == expected


def test_tolerance_is_relative_for_stl2_and_a_distance_for_db(config):
    # an angular tolerance this wide leaves it to the tolerance to decide on the number of triangles
    config.update(tolerance=0.01, angular_tolerance=1.0)
    triangles = {}
    for radius in (5.0, 50.0):
        assembly = imported([("iron_ball", models.cq.Workplane("XY").sphere(radius).val())])
        for backend in ("stl2", "db"):
            assembly.solids_to_h5m(backend=backend, h5m_filename="model.h5m")
            triangles[backend, radius] = assembly.meshed_model.n_triangles
    assert triangles["stl2", 50.0] == triangles["stl2", 5.0]
    assert triangles["db", 50.0] > 2 * triangles["db", 5.0]


def _lose_one_face(monkeypatch):
    """Make the triangulation of the first face of the first solid go missing."""
    from OCP.BRepTools import BRepTools

    original = occ_mesher.triangulate

    def triangulate(shapes, *args, **kwargs):
        original(shapes, *args, **kwargs)
        BRepTools.Clean_s(occ_mesher.collect_faces(shapes[:1]).faces[0])

    monkeypatch.setattr(occ_mesher, "triangulate", triangulate)


def test_face_without_triangles_is_an_error(config, monkeypatch, tmp_path):
    assembly = imported(models.separate_boxes(2))
    _lose_one_face(monkeypatch)
    with pytest.raises(MeshingError, match=r"1 face could not be triangulated, in solid\(s\) 1"):
        assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")
    assert not (tmp_path / "model.h5m").exists()


def test_open_volume_is_written_only_on_request(config, monkeypatch, tmp_path, capsys):
    assembly = imported(models.separate_boxes(2))
    _lose_one_face(monkeypatch)
    config["strict"] = False
    assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")

    assert (tmp_path / "model.h5m").exists()
    report = assembly.validation_report
    assert [v.id for v in report.leaky_volumes] == [1] and not report.fit_for_transport
    assert re.search(r"WARNING: +volume 1 \(material 'iron'.*\) is not closed: 4 open", capsys.readouterr().out)


def test_open_volume_is_an_error(config, monkeypatch, tmp_path):
    assembly = imported(models.separate_boxes(2))
    # every face is triangulated, but the check finds one triangle less than there should be
    original = occ_mesher.read_triangulations

    def read_triangulations(faces, *args, **kwargs):
        mesh = original(faces, *args, **kwargs)
        mesh.triangles = mesh.triangles[1:]
        mesh.offsets = np.concatenate(([0], mesh.offsets[1:] - 1))
        return mesh

    monkeypatch.setattr(occ_mesher, "read_triangulations", read_triangulations)
    with pytest.raises(MeshValidationError, match="volume 1 .* is not closed: 3 open") as error:
        assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")
    assert [v.id for v in error.value.report.leaky_volumes] == [1]
    assert not (tmp_path / "model.h5m").exists()


def test_nothing_to_mesh_is_an_error(config):
    with pytest.raises(RuntimeError, match="no solids"):
        ab.Assembly(verbose=0).solids_to_h5m(backend="stl2", h5m_filename="model.h5m")


def test_refine_is_reported_as_having_no_effect(config, capsys):
    # asking for refinement used to end the stl2 backend with an AttributeError
    config["refine"] = 1
    assembly = imported(models.separate_boxes(1), merge=False)
    assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")
    assert "mesher_config['refine'] has no effect" in capsys.readouterr().out
    assert assembly.validation_report.ok


def test_refinement_is_not_carried_over_to_the_next_run(config, capsys):
    pytest.importorskip("pymoab")
    if shutil.which("mmgs_O3"):
        pytest.skip("can only be seen when the refinement tool mmgs_O3 is missing")
    assembly = imported(models.separate_boxes(1), merge=False)
    # the mesher object is kept between runs, and used to keep the settings of its first run
    for refine, tried in ((1, True), (0, False)):
        config["refine"] = refine
        assembly.solids_to_h5m(backend="stl", h5m_filename="model.h5m")
        assert ("Cannot find mmgs" in capsys.readouterr().out) == tried


def test_stl2_works_without_pymoab(tmp_path):
    # pymoab cannot be installed from PyPI
    models.write_step(models.separate_boxes(2), tmp_path / "boxes.step")
    script = textwrap.dedent(
        """
        import sys
        sys.modules["pymoab"] = None  # makes every import of pymoab fail
        import CAD_to_OpenMC.assembly as ab
        from CAD_to_OpenMC.h5m_io import read_h5m
        from CAD_to_OpenMC.validate import check_model

        a = ab.Assembly(["boxes.step"], verbose=0)
        a.run(backend="stl2", h5m_filename="boxes.h5m")
        assert check_model(read_h5m("boxes.h5m")).ok
        try:
            a.solids_to_h5m(backend="stl", h5m_filename="other.h5m")
        except ImportError as error:
            assert "pymoab" in str(error), error
        else:
            raise AssertionError("the stl backend should have asked for pymoab")
        """
    )
    done = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_merge2h5m_joins_the_meshes_of_assemblies(config):
    models.write_step(models.pellet_stack(2), "pellets.step")
    models.write_step([(name, solid.translate((50, 0, 0))) for name, solid in models.separate_boxes(2)], "boxes.step")
    first = ab.Assembly(["pellets.step"], verbose=0, implicit_complement="air")
    first.run(backend="stl2", h5m_filename="pellets.h5m")
    second = ab.Assembly(["boxes.step"], verbose=0, implicit_complement="water")
    second.run(backend="db", h5m_filename="boxes.h5m")

    ab.merge2h5m([first, second], h5m_file="both.h5m", vtk=False, verbose=0)

    model = read_h5m("both.h5m")
    assert model.volume_tags == ["clad", "fuel", "fuel", "iron", "iron"]
    assert model.volume_ids.tolist() == [1, 2, 3, 4, 5]
    assert model.n_surfaces == first.meshed_model.n_surfaces + second.meshed_model.n_surfaces
    assert model.implicit_complement == "water"
    assert check_model(model).ok
