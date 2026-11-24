"""The vtk export: the file has to hold the mesh it was given."""
import numpy as np

from CAD_to_OpenMC.vtk_io import write_vtk
from tests.test_validate import two_tetrahedra


def block(raw, header, dtype, count):
    """The numbers that follow a header line of a binary vtk-file."""
    start = raw.index(header) + len(header)
    return np.frombuffer(raw, dtype, count, start)


def test_vtk_file_holds_points_triangles_and_ids(tmp_path):
    model = two_tetrahedra(shared=True)
    model.volume_ids = np.array([7, 9])
    raw = open(write_vtk(model, tmp_path / "model.vtk"), "rb").read()

    assert raw.startswith(b"# vtk DataFile Version 3.0\n")
    assert np.array_equal(block(raw, b"POINTS 5 double\n", ">f8", 15).reshape(5, 3), model.vertices)
    cells = block(raw, b"POLYGONS 7 28\n", ">i4", 28).reshape(7, 4)
    assert np.all(cells[:, 0] == 3) and np.array_equal(cells[:, 1:], model.triangles)

    def scalars(name):
        return block(raw, f"SCALARS {name} int 1\nLOOKUP_TABLE default\n".encode(), ">i4", 7).tolist()

    # the first surface, of one triangle, lies between the two volumes
    assert scalars("surface_id") == [1, 2, 2, 2, 3, 3, 3]
    assert scalars("forward_volume") == [7, 7, 7, 7, 9, 9, 9]
    assert scalars("reverse_volume") == [9, 0, 0, 0, 0, 0, 0]
