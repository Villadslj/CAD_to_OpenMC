"""Merging: solids that touch get shared faces, solids that overlap are told apart."""
import cadquery as cq
import pytest
from OCP.BRepCheck import BRepCheck_Analyzer

import CAD_to_OpenMC.assembly as ab
from CAD_to_OpenMC.meshed_model import NO_VOLUME
from CAD_to_OpenMC.merge import connect_solids, find_overlaps
from CAD_to_OpenMC.occ_mesher import collect_faces
from tests import models


def box(x0, x1, y=(0, 10), z=(0, 10)):
    """A box from x0 to x1, in mm."""
    solid = cq.Workplane("XY").box(x1 - x0, y[1] - y[0], z[1] - z[0], centered=False).val()
    return solid.translate((x0, y[0], z[0]))


def volumes(pieces):
    return [[cq.Shape.cast(piece).Volume() for piece in group] for group in pieces]


def test_touching_solids_stay_in_the_order_they_were_given():
    # three boxes of the same size: only their place tells them apart
    connected = connect_solids([box(20, 30), box(0, 10), box(10, 20)])

    assert not connected.overlaps
    assert [len(group) for group in connected.pieces] == [1, 1, 1]
    solids = [group[0] for group in connected.pieces]
    assert [cq.Shape.cast(s).Center().x for s in solids] == pytest.approx([25, 5, 15])
    # the box in the middle shares a face with either neighbour
    senses = collect_faces(solids).senses
    assert sorted(map(tuple, senses[senses[:, 1] != NO_VOLUME].tolist())) == [(0, 2), (1, 2)]


def test_overlap_goes_to_the_smaller_solid_and_nothing_is_lost():
    connected = connect_solids([box(0, 10), box(8, 12)])

    (overlap,) = connected.overlaps
    assert (overlap.solids, overlap.owner) == ((0, 1), 1)
    assert overlap.volume == pytest.approx(200)
    # the large box ends where the small one begins. The small one is whole: the region it has
    # in common with the large one and the rest of it are one solid again, without the face between them
    assert volumes(connected.pieces) == [pytest.approx([800]), pytest.approx([400])]
    (small,) = connected.pieces[1]
    assert len(cq.Shape.cast(small).Faces()) == 10
    assert BRepCheck_Analyzer(small).IsValid()


def test_solid_inside_another_takes_its_place_there():
    connected = connect_solids([box(0, 10), box(2, 4, y=(2, 4), z=(2, 4))])

    assert volumes(connected.pieces) == [pytest.approx([992]), pytest.approx([8])]
    assert [(o.solids, o.owner) for o in connected.overlaps] == [((0, 1), 1)]


def test_second_of_two_identical_solids_is_left_without_a_piece():
    connected = connect_solids([box(0, 10), box(0, 10)])

    assert volumes(connected.pieces) == [pytest.approx([1000]), []]
    assert [(o.solids, o.owner) for o in connected.overlaps] == [((0, 1), 0)]


def stud_through_a_wall():
    """A stud that sticks into the solids on either side of a wall, without touching any of their faces."""
    return [box(0, 10), box(8, 12, y=(4, 6), z=(4, 6)), box(10, 30)]


def test_find_overlaps_finds_an_overlap_without_coinciding_faces():
    # connect_solids returns these three solids as they are
    overlaps = find_overlaps(stud_through_a_wall())

    assert sorted((o.solids, o.owner) for o in overlaps) == [((0, 1), 1), ((1, 2), 1)]
    assert [o.volume for o in overlaps] == pytest.approx([8, 8])
    assert find_overlaps([box(0, 10), box(10, 20), box(30, 40)]) == []


def test_solid_cut_in_two_comes_back_as_two():
    # a slice of the same cross-section as the block it lies in
    block, slice_ = box(0, 30), box(14, 16)
    connected = connect_solids([block, slice_])

    assert volumes(connected.pieces) == [pytest.approx([1400, 1400]), pytest.approx([200])]
    # either part of the block shares the face where it ends with the slice
    solids = connected.pieces[0] + connected.pieces[1]
    senses = collect_faces(solids).senses
    assert sorted(map(tuple, senses[senses[:, 1] != NO_VOLUME].tolist())) == [(0, 2), (1, 2)]


def test_assembly_reports_overlaps_and_converts_without_losing_volume(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    models.write_step([("steel_block", box(0, 30)), ("copper_slice", box(14, 16))], "model.step")
    assembly = ab.Assembly(["model.step"], verbose=0)
    assembly.import_stp_files()
    assembly.merge_all()

    # the slice is whole, what is left of the block on either side of it is a volume each
    assert [e.tag for e in assembly.entities] == ["steel", "steel", "copper"]
    assert [(o.solids, o.owner) for o in assembly.overlaps] == [((0, 1), 1)]
    assert (
        "WARNING: solids 1 ('steel_block') and 2 ('copper_slice') overlap in a region of volume 0.2. "
        "The region is given to solid 2."
    ) in capsys.readouterr().out

    assembly.solids_to_h5m(backend="stl2", h5m_filename="model.h5m")
    report = assembly.validation_report
    assert report.ok
    assert [v.mesh_volume for v in report.volumes] == pytest.approx([1.4, 1.4, 0.2])


def test_assembly_finds_overlaps_without_changing_the_solids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    names = ["steel_plate", "copper_stud", "water_tank"]
    models.write_step(list(zip(names, stud_through_a_wall())), "model.step")
    assembly = ab.Assembly(["model.step"], verbose=0)
    assembly.import_stp_files()

    overlaps = assembly.find_overlaps()

    assert sorted(o.solids for o in overlaps) == [(0, 1), (1, 2)]
    assert [o.volume for o in overlaps] == pytest.approx([0.008, 0.008])
    assert [e.volume for e in assembly.entities] == pytest.approx([1.0, 0.016, 2.0])
