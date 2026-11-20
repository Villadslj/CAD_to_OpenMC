"""Export of a meshed model to a VTK file, to look at the mesh in e.g. ParaView."""
from __future__ import annotations

import numpy as np

from .meshed_model import MeshedModel, NO_VOLUME


def write_vtk(model: MeshedModel, filename) -> str:
    """Write the triangles of a model as VTK polydata (legacy binary format).

    Every triangle carries the id of its surface and the ids of the volumes on
    either side of it (0 where there is none), so single volumes can be picked
    out with a threshold filter.
    """
    n_triangles = model.n_triangles
    cells = np.empty((n_triangles, 4), dtype=">i4")
    cells[:, 0] = 3
    cells[:, 1:] = model.triangles
    surface = model.triangle_surfaces()
    senses = model.surface_senses[surface]
    volumes = np.where(senses == NO_VOLUME, 0, model.volume_ids[np.maximum(senses, 0)])
    cell_data = (
        ("surface_id", model.surface_ids[surface]),
        ("forward_volume", volumes[:, 0]),
        ("reverse_volume", volumes[:, 1]),
    )
    with open(filename, "wb") as f:
        f.write(b"# vtk DataFile Version 3.0\n")
        f.write(b"DAGMC surface mesh written by CAD_to_OpenMC\n")
        f.write(b"BINARY\nDATASET POLYDATA\n")
        f.write(f"POINTS {model.n_vertices} double\n".encode())
        f.write(model.vertices.astype(">f8").tobytes())
        f.write(f"\nPOLYGONS {n_triangles} {4 * n_triangles}\n".encode())
        f.write(cells.tobytes())
        f.write(f"\nCELL_DATA {n_triangles}\n".encode())
        for name, values in cell_data:
            f.write(f"SCALARS {name} int 1\nLOOKUP_TABLE default\n".encode())
            f.write(np.asarray(values).astype(">i4").tobytes())
            f.write(b"\n")
    return str(filename)
