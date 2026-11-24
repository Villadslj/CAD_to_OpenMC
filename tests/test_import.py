"""Import of step-files into an Assembly: the solids, the names of their parts and the material tags."""
import pathlib

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


@pytest.mark.parametrize("unit, declaration", [("M", "SI_UNIT($,.METRE.)"), ("INCH", "CONVERSION_BASED_UNIT('INCH'")])
def test_file_in_other_length_units_comes_in_as_cm(unit, declaration, tmp_path):
    path = tmp_path / "box.step"
    Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(cq.Workplane("XY").box(10, 20, 30, centered=False).val().wrapped, STEPControl_AsIs)
        writer.Write(str(path))
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
    # the box of 10 x 20 x 30 mm is in the file in the other unit
    assert declaration in path.read_text()

    (entity,) = imported(path).entities
    assert (entity.bb.xlen, entity.bb.ylen, entity.bb.zlen) == pytest.approx((1.0, 2.0, 3.0), abs=1e-6)
    assert entity.volume == pytest.approx(6.0)


def test_properties_of_an_entity_follow_its_solid():
    entity = ab.Entity(solid=cq.Workplane("XY").box(1, 2, 3).val())
    assert entity.volume == pytest.approx(6.0)
    # merge_all replaces the solid
    entity.solid = cq.Workplane("XY").box(2, 2, 3, centered=False).val()
    assert entity.volume == pytest.approx(12.0)
    assert entity.bb.xlen == pytest.approx(2.0)
    assert entity.center.toTuple() == pytest.approx((1.0, 1.0, 1.5))
