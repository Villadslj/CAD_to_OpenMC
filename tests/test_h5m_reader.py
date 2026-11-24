"""The h5m reader, checked against a file written through MOAB by the STL-based pipeline."""
import pathlib

import pytest

pytest.importorskip("pymoab")

import CAD_to_OpenMC.assembly as ab
from CAD_to_OpenMC.h5m_io import read_h5m
from CAD_to_OpenMC.validate import check_model

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = dict(ab.mesher_config)
    yield tmp_path
    ab.mesher_config.clear()
    ab.mesher_config.update(saved)


def test_reads_geometry_written_by_moab(workdir):
    a = ab.Assembly([str(ROOT / "tests" / "7pin.step")], verbose=0)
    a.implicit_complement = "water"
    a.run(backend="stl2", merge=True, h5m_filename="pins.h5m")

    model = read_h5m("pins.h5m")
    assert model.volume_tags == ["fuel"] * 7 + ["graphite"] * 7
    assert model.volume_ids.tolist() == list(range(1, 15))
    assert model.implicit_complement == "water"
    assert model.faceting_tolerance == ab.mesher_config["tolerance"]
    assert len(set(model.surface_ids.tolist())) == model.n_surfaces
    # every fuel pin shares exactly one surface with a graphite sleeve of its own
    shared = model.surface_senses[model.surface_senses[:, 1] >= 0]
    assert sorted(shared[:, 0].tolist()) == list(range(7))
    assert sorted(shared[:, 1].tolist()) == list(range(7, 14))
    assert check_model(model).ok
