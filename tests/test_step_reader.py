"""The names of the parts in a step-file, which the material tags are taken from."""
import pathlib

import cadquery as cq
import pytest

from CAD_to_OpenMC.step_reader import StepFile
from tests import models

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = [
    "tests/7pin.step",
    "examples/step_files/pincell1.step",
    "examples/step_files/pincell2.step",
    "examples/step_files/reactor_test1.step",
    "examples/step_files/toroids.step",
    "examples/godiva_iv/GIV_BR_detailed.step",
    "examples/godiva_iv/GIV_CR_simplified.step",
    "examples/godiva_iv/GIV_core_simplified.step",
]


def names_from_gmsh(path):
    """The names as they used to be found: the last part of the label gmsh gives to every volume."""
    try:
        import gmsh
    except (ImportError, OSError) as error:
        pytest.skip(f"gmsh is not usable: {error}")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        volumes = gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()
        return [gmsh.model.getEntityName(3, tag).split("/")[-1] for _, tag in volumes]
    finally:
        gmsh.finalize()


def nested_assembly(path):
    """A plate that carries three copies of a cell, the cell being an assembly of two parts itself."""
    cell = cq.Assembly(name="cell")
    cell.add(cq.Workplane("XY").circle(4).extrude(40).val(), name="fuel_pin", loc=cq.Location((0, 0, 5)))
    cell.add(cq.Workplane("XY").circle(5).circle(4).extrude(40).val(), name="clad_tube", loc=cq.Location((0, 0, 5)))
    reactor = cq.Assembly(name="reactor")
    reactor.add(cq.Workplane("XY").box(100, 30, 5, centered=(False, True, False)).val(), name="steel_plate")
    for i in range(3):
        reactor.add(cell, name=f"cell_{i}", loc=cq.Location((20 + 30 * i, 0, 0)))
    reactor.export(str(path))


@pytest.mark.parametrize("sample", SAMPLES)
def test_names_in_example_files_are_those_gmsh_reports(sample):
    step = StepFile(ROOT / sample)
    names = step.solid_names()
    assert names and all(names)
    assert names == names_from_gmsh(ROOT / sample)


@pytest.mark.parametrize("sample", SAMPLES)
def test_solids_are_those_cadquery_imports(sample):
    ours = [solid for shape in StepFile(ROOT / sample).shapes for solid in cq.Shape.cast(shape).Solids()]
    theirs = [solid for shape in cq.importers.importStep(str(ROOT / sample)).vals() for solid in shape.Solids()]
    assert len(ours) == len(theirs) > 0
    for a, b in zip(ours, theirs):
        assert a.Volume() == pytest.approx(b.Volume(), rel=1e-12)
        assert a.Center().toTuple() == pytest.approx(b.Center().toTuple(), rel=1e-12, abs=1e-12)


def test_parts_of_nested_assemblies_are_named_after_their_product(tmp_path):
    nested_assembly(tmp_path / "nested.step")
    names = StepFile(tmp_path / "nested.step").solid_names()
    assert names == ["steel_plate"] + ["fuel_pin", "clad_tube"] * 3
    assert names == names_from_gmsh(tmp_path / "nested.step")


def test_copies_of_a_part_are_named_after_the_part(tmp_path):
    # every cell of this lattice is a copy of the same three solids
    models.write_step(models.instanced_pin_lattice(2, 2), tmp_path / "lattice.step")
    names = StepFile(tmp_path / "lattice.step").solid_names()
    assert names == ["fuel_0_0", "clad_0_0", "water_0_0"] * 4
    assert names == names_from_gmsh(tmp_path / "lattice.step")
