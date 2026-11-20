"""The step-files that come with the package have to give volumes that are closed."""
import os
import pathlib

import pytest

import CAD_to_OpenMC.assembly as ab
from CAD_to_OpenMC.h5m_io import read_h5m
from CAD_to_OpenMC.validate import check_model

ROOT = pathlib.Path(__file__).resolve().parent.parent
slow = pytest.mark.skipif(not os.environ.get("C2O_SLOW_TESTS"), reason="takes a minute, set C2O_SLOW_TESTS=1 to run")

# (step-file, number of solids)
EXAMPLES = [
    ("tests/7pin.step", 14),
    ("examples/step_files/pincell1.step", 3),
    ("examples/step_files/pincell2.step", 5),
    ("examples/step_files/reactor_test1.step", 5),
    ("examples/step_files/toroids.step", 3),
    ("examples/step_files/spheroids.step", 3),
    ("examples/step_files/oblate_ellipsoids.step", 3),
    ("examples/godiva_iv/GIV_BR_detailed.step", 2),
    ("examples/godiva_iv/GIV_CR_detailed.step", 1),
    ("examples/godiva_iv/GIV_core_simplified.step", 17),
    pytest.param("examples/godiva_iv/GIV_core_detailed.step", 39, marks=slow),
]


@pytest.mark.parametrize("step, n_solids", EXAMPLES)
@pytest.mark.parametrize("backend", ["stl2", "db"])
def test_example_gives_closed_volumes(backend, step, n_solids, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assembly = ab.Assembly([str(ROOT / step)], verbose=0)
    assembly.import_stp_files()
    assembly.merge_all()
    assembly.solids_to_h5m(backend=backend, h5m_filename="out.h5m")

    model = read_h5m("out.h5m")
    assert model.n_volumes == n_solids
    report = check_model(model, cad_volumes=[e.solid.Volume() for e in assembly.entities])
    assert report.fit_for_transport, report.summary()
    assert max(abs(v.volume_error) for v in report.volumes) < 0.02
