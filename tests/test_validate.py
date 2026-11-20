import numpy as np
import pytest

from CAD_to_OpenMC.meshed_model import MeshedModel, NO_VOLUME
from CAD_to_OpenMC.validate import check_model

# a tetrahedron above the xy-plane and its mirror image below it. All triangles
# are listed with their normal (right hand rule) pointing out of the tetrahedron.
VERTICES = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, -1]], dtype=float)
BASE = [0, 2, 1]  # normal -z: out of the upper, into the lower tetrahedron
UPPER_SIDES = [[0, 1, 3], [1, 2, 3], [0, 3, 2]]
LOWER_SIDES = [[0, 4, 1], [0, 2, 4], [1, 4, 2]]


def tetrahedron(triangles=None):
    triangles = [BASE] + UPPER_SIDES if triangles is None else triangles
    return MeshedModel(VERTICES.copy(), triangles, [0, len(triangles)], [[0, NO_VOLUME]], ["steel"])


def two_tetrahedra(shared):
    """Two tetrahedra on either side of the xy-plane, with or without a shared base surface."""
    if shared:
        triangles = [BASE] + UPPER_SIDES + LOWER_SIDES
        senses = [[0, 1], [0, NO_VOLUME], [1, NO_VOLUME]]
        return MeshedModel(VERTICES.copy(), triangles, [0, 1, 4, 7], senses, ["a", "b"])
    triangles = [BASE] + UPPER_SIDES + [BASE[::-1]] + LOWER_SIDES
    return MeshedModel(VERTICES.copy(), triangles, [0, 4, 8], [[0, NO_VOLUME], [1, NO_VOLUME]], ["a", "b"])


def test_closed_volume_passes():
    report = check_model(tetrahedron(), cad_volumes=[1 / 6], volume_rtol=1e-12)
    assert report.ok
    assert report.volumes[0].mesh_volume == pytest.approx(1 / 6)


def test_missing_triangle_is_reported():
    report = check_model(tetrahedron([BASE] + UPPER_SIDES[:2]))
    assert report.volumes[0].open_edges == 3
    assert [v.id for v in report.leaky_volumes] == [1]
    assert not report.ok


def test_flipped_triangle_is_reported():
    report = check_model(tetrahedron([BASE[::-1]] + UPPER_SIDES))
    volume = report.volumes[0]
    assert volume.closed and volume.misoriented_edges == 3
    assert not report.ok


def test_inside_out_volume_is_reported():
    inverted = [t[::-1] for t in [BASE] + UPPER_SIDES]
    report = check_model(tetrahedron(inverted))
    volume = report.volumes[0]
    assert volume.closed and volume.misoriented_edges == 0 and volume.mesh_volume < 0
    assert not report.ok


def test_shared_surface_bounds_both_volumes():
    report = check_model(two_tetrahedra(shared=True))
    assert report.ok
    assert [v.mesh_volume for v in report.volumes] == pytest.approx([1 / 6, 1 / 6])


def test_coincident_surfaces_that_are_not_shared_are_reported():
    report = check_model(two_tetrahedra(shared=False))
    assert all(v.closed and v.oriented for v in report.volumes)
    assert report.duplicate_surface_pairs == [(1, 2, 1)]
    assert not report.ok


def test_vertices_are_matched_by_coordinates():
    # every triangle with its own copy of the vertices, as in files assembled from STL
    triangles = np.array([BASE] + UPPER_SIDES)
    model = MeshedModel(VERTICES[triangles.ravel()], np.arange(12).reshape(4, 3), [0, 4], [[0, NO_VOLUME]], ["steel"])
    assert check_model(model).ok


def test_wrong_cad_volume_is_reported():
    report = check_model(tetrahedron(), cad_volumes=[0.2], volume_rtol=0.05)
    assert report.volumes[0].volume_error == pytest.approx(-1 / 6)
    assert not report.ok


def test_concatenate_keeps_models_apart_and_renumbers():
    first, second = tetrahedron(), two_tetrahedra(shared=True)
    second.vertices += [5.0, 0.0, 0.0]
    joined = MeshedModel.concatenate([first, second])
    assert joined.volume_tags == ["steel", "a", "b"]
    assert joined.volume_ids.tolist() == [1, 2, 3] and joined.surface_ids.tolist() == [1, 2, 3, 4]
    assert joined.surface_senses.tolist() == [[0, NO_VOLUME], [1, 2], [1, NO_VOLUME], [2, NO_VOLUME]]
    report = check_model(joined)
    assert report.ok
    assert [v.mesh_volume for v in report.volumes] == pytest.approx([1 / 6] * 3)
