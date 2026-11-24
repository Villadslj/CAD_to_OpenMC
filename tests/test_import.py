"""Import of step-files into an Assembly: the solids, the names of their parts and the material tags."""
import pathlib
import re

import cadquery as cq
import pytest
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

import CAD_to_OpenMC.assembly as ab
from tests import models

ROOT = pathlib.Path(__file__).resolve().parent.parent
PINCELL = ROOT / "examples" / "step_files" / "pincell1.step"


def imported(path, names_from="step", **kwargs):
    assembly = ab.Assembly([str(path)], verbose=0)
    assembly.names_from = names_from
    assembly.import_stp_files(**kwargs)
    return assembly


def box(x):
    return cq.Workplane("XY").center(x, 0).box(10, 10, 10).val()


@pytest.mark.parametrize(
    "sample",
    ["tests/7pin.step", "examples/step_files/pincell2.step", "examples/godiva_iv/GIV_core_simplified.step"],
)
def test_tags_are_those_found_through_gmsh(sample):
    if ab.nogmsh:
        pytest.skip("gmsh is not usable")
    from_step = imported(ROOT / sample)
    from_gmsh = imported(ROOT / sample, names_from="gmsh")
    assert [e.name for e in from_step.entities] == [e.name for e in from_gmsh.entities]
    assert [e.tag for e in from_step.entities] == [e.tag for e in from_gmsh.entities]
    assert len({e.tag for e in from_step.entities}) > 1


def test_bodies_of_a_part_get_the_tag_of_the_part(tmp_path):
    # gmsh reports no name for the solids of a part with several bodies, which left them with the default tag
    pair = cq.Compound.makeCompound([box(0), box(20)])
    models.write_step([("steel_pair", pair), ("water_block", box(40))], tmp_path / "bodies.step")
    assembly = imported(tmp_path / "bodies.step")
    assert [e.tag for e in assembly.entities] == ["steel", "steel", "water"]


def test_skipping_a_volume_keeps_the_tags_of_the_others():
    tags = [e.tag for e in imported(PINCELL).entities]
    assert len(set(tags)) == 3
    # the tags used to shift: the volumes that were kept got the tags of the first volumes in the file
    assert [e.tag for e in imported(PINCELL, vol_skip=[1]).entities] == tags[1:]
    assert [e.tag for e in imported(PINCELL, vol_skip=[2]).entities] == [tags[0], tags[2]]


@pytest.mark.parametrize(
    "unit, declaration, cm_per_unit",
    [("M", "SI_UNIT($,.METRE.)", 100.0), ("INCH", "CONVERSION_BASED_UNIT('INCH'", 2.54)],
)
def test_file_in_other_length_units_comes_in_as_cm(unit, declaration, cm_per_unit, tmp_path):
    path = tmp_path / "box.step"
    # the parameter only exists once a writer has been created
    writer = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer.Transfer(cq.Workplane("XY").box(10, 20, 30, centered=False).val().wrapped, STEPControl_AsIs)
        writer.Write(str(path))
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
    text = path.read_text()
    assert declaration in text

    # OCC 7.7 converts the coordinates to the unit, 7.8 writes them unchanged: take the size from the file.
    points = [
        [float(v) for v in p.split(",")]
        for p in re.findall(r"CARTESIAN_POINT\('[^']*',\(([^()]*)\)\)", text)
    ]
    size = [max(p[i] for p in points if len(p) == 3) * cm_per_unit for i in range(3)]

    (entity,) = imported(path).entities
    assert (entity.bb.xlen, entity.bb.ylen, entity.bb.zlen) == pytest.approx(size, rel=1e-6)
    assert entity.volume == pytest.approx(size[0] * size[1] * size[2], rel=1e-6)


def test_session_length_unit_does_not_scale_the_import(tmp_path):
    path = tmp_path / "box.step"
    cq.exporters.export(cq.Workplane("XY").box(10, 20, 30), str(path))
    STEPControl_Writer()
    assert Interface_Static.SetCVal_s("xstep.cascade.unit", "CM")
    try:
        (entity,) = imported(path).entities
    finally:
        Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    assert (entity.bb.xlen, entity.bb.ylen, entity.bb.zlen) == pytest.approx((1.0, 2.0, 3.0))


def test_gmsh_backend_leaves_the_session_length_unit(tmp_path, monkeypatch):
    if ab.nogmsh:
        pytest.skip("gmsh is not usable")
    # gmsh used to leave it at cm, which made every later import 10 times too small
    monkeypatch.chdir(tmp_path)
    cq.exporters.export(cq.Workplane("XY").box(10, 20, 30), "box.step")
    STEPControl_Writer()
    before = Interface_Static.CVal_s("xstep.cascade.unit")
    ab.Assembly(["box.step"], verbose=0).run(backend="gmsh", h5m_filename="box.h5m")
    assert Interface_Static.CVal_s("xstep.cascade.unit") == before


def test_properties_of_an_entity_follow_its_solid():
    entity = ab.Entity(solid=cq.Workplane("XY").box(1, 2, 3).val())
    assert entity.volume == pytest.approx(6.0)
    # merge_all replaces the solid
    entity.solid = cq.Workplane("XY").box(2, 2, 3, centered=False).val()
    assert entity.volume == pytest.approx(12.0)
    assert entity.bb.xlen == pytest.approx(2.0)
    assert entity.center.toTuple() == pytest.approx((1.0, 1.0, 1.5))
