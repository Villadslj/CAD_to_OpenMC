"""Triangulation with OpenCASCADE: closed volumes, and how their surfaces are joined along the edges."""
import cadquery as cq
import numpy as np
import pytest
from OCP.BOPAlgo import BOPAlgo_MakeConnected
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Trsf, gp_Vec
from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid

from CAD_to_OpenMC.meshed_model import NO_VOLUME
from CAD_to_OpenMC.occ_mesher import mesh_and_check
from CAD_to_OpenMC.options import MeshOptions
from tests import models


def connected(parts):
    """The solids of the parts, in cm, made to share the faces where they touch."""
    builder = BOPAlgo_MakeConnected()
    for _, solid in parts:
        builder.AddArgument(solid.scale(0.1).wrapped)
    builder.Perform()
    return cq.Compound(builder.Shape()).Solids()


@pytest.mark.parametrize("grouping", ["model", "solid"])
@pytest.mark.parametrize("reverse", [False, True])
def test_solids_that_share_faces_give_closed_volumes_in_any_order(grouping, reverse):
    # A cladding tube around three pellets. Meshed one solid at a time with the cladding first, the
    # cladding is left open with these tolerances: its edges get divided coarser than the pellets need.
    solids = connected(models.pellet_stack())
    solids = solids[::-1] if reverse else solids
    options = MeshOptions(tolerance=0.001, angular_tolerance=0.2, grouping=grouping)

    model, report = mesh_and_check(
        solids, ["m"] * len(solids), options, cad_volumes=[s.Volume() for s in solids], volume_rtol=0.01
    )

    assert report.ok, report.summary()
    # the three pellets share their side with the cladding and their ends with each other
    assert np.count_nonzero(model.surface_senses[:, 1] != NO_VOLUME) == 5


def test_mesh_does_not_depend_on_the_number_of_threads():
    shells = [(name, solid.translate((100, 0, 0))) for name, solid in models.nested_shells(2)]
    solids = connected(models.pin_lattice(2) + shells)
    tags = ["m"] * len(solids)

    meshes = [mesh_and_check(solids, tags, MeshOptions(tolerance=0.001, threads=n))[0] for n in (1, 2, 4)]

    assert [mesh.info["threads"] for mesh in meshes] == [1, 2, 4]
    for other in meshes[1:]:
        for name in ("vertices", "triangles", "surface_offsets", "surface_senses"):
            assert np.array_equal(getattr(meshes[0], name), getattr(other, name)), name


def box():
    return cq.Workplane("XY").box(10, 7, 5, centered=False).val()


def test_faces_placed_by_different_locations_are_joined_along_their_edges():
    # two boxes that touch, each put in place by a location of its own, made to share the face between them
    place = cq.Location(cq.Vector(1.1, 2.3, 3.7), cq.Vector(1, 2, 3), 40.0)
    builder = BOPAlgo_MakeConnected()
    for solid in (box().moved(place), box().moved(place * cq.Location(cq.Vector(10, 0, 0)))):
        builder.AddArgument(solid.wrapped)
    builder.Perform()
    solids = cq.Compound(builder.Shape()).Solids()

    model, report = mesh_and_check(solids, ["iron", "lead"], MeshOptions())

    # the same corner comes back from differently placed faces with coordinates that differ in the last digit
    assert model.info["located_faces"] > 0
    assert model.info["vertices_joined_along_edges"] > 0
    assert report.ok
    assert (model.n_volumes, model.n_surfaces, model.n_vertices) == (2, 11, 12)


def box_with_loose_face(distance):
    """A box of which one face is not connected to the others: its edges and vertices are its own, a distance away."""
    faces = [face.wrapped for face in box().Faces()]
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(distance, 0, 0))
    faces[0] = BRepBuilderAPI_Transform(faces[0], shift, True).Shape()
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for face in faces:
        builder.Add(shell, face)
    solid = TopoDS_Solid()
    builder.MakeSolid(solid)
    builder.Add(solid, shell)
    return solid


def test_vertices_that_coincide_within_the_tolerance_of_the_cad_model_are_joined():
    # the vertices of a box have a tolerance of 1e-7
    model, report = mesh_and_check([box_with_loose_face(1e-9)], ["iron"], MeshOptions())

    assert report.ok
    assert model.info["vertices_joined_across_gaps"] == 4
    assert model.info["largest_gap_joined"] == pytest.approx(1e-9, rel=1e-3)
    assert model.n_vertices == 8


def test_gap_wider_than_the_tolerance_of_the_cad_model_is_reported():
    model, report = mesh_and_check([box_with_loose_face(1e-3)], ["iron"], MeshOptions())

    assert model.info["vertices_joined_across_gaps"] == 0
    volume = report.volumes[0]
    # the loose face and the hole it leaves have four edges each
    assert not volume.closed and volume.open_edges == 8
    assert volume.open_edge_at is not None
    assert not report.fit_for_transport
