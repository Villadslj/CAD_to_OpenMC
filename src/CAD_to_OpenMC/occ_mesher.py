"""Triangulation of CAD solids with OpenCASCADE, kept in memory.

The solids are triangulated by OpenCASCADE's incremental mesher and the result
is collected in a MeshedModel, without intermediate files.

* A face that belongs to two solids is recognised through the topology. It is
  triangulated once and both volumes refer to it.
* All solids are handed to the mesher together. Every edge is then divided
  exactly once, so the triangles on either side of an edge always fit, and the
  mesher can work on all faces in parallel.
* Vertices with identical coordinates are joined, so neighbouring surfaces
  refer to the same vertices along their common edge. Where the coordinates of
  the same point differ in the last digits, which happens for parts that are
  put in place by a location of a CAD assembly, the points are joined through
  the topology instead: OpenCASCADE knows which nodes lie on which edge.
"""
from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np
from OCP.BinTools import BinTools
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.IMeshTools import (
    IMeshTools_MeshAlgoType_Delabella,
    IMeshTools_MeshAlgoType_Watson,
    IMeshTools_Parameters,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Face
from OCP.TopTools import TopTools_IndexedMapOfShape

from .meshed_model import MeshedModel, NO_VOLUME
from .options import MeshOptions
from .threads import occ_threads
from .validate import check_model, weld_vertices

_ALGORITHMS = {
    "watson": IMeshTools_MeshAlgoType_Watson,
    "delabella": IMeshTools_MeshAlgoType_Delabella,
}


class MeshingError(RuntimeError):
    """The solids could not be turned into a usable surface mesh."""


@dataclass
class FaceTable:
    """The distinct faces of a list of solids and the solids they bound."""

    faces: list
    #: (n_faces, 2) [first solid, second solid or NO_VOLUME]
    senses: np.ndarray
    #: True where the face is reversed in its first solid: its triangles then
    #: have to be turned around to point out of that solid
    reversed_in_first: np.ndarray
    problems: List[str] = field(default_factory=list)


def _unwrap(shape):
    """The TopoDS_Shape of a cadquery object, or the object itself."""
    return getattr(shape, "wrapped", shape)


def collect_faces(shapes: Sequence) -> FaceTable:
    """Find the distinct faces of the solids and the solids on either side of them."""
    face_map = TopTools_IndexedMapOfShape()
    faces, first, second, reversed_in_first, problems = [], [], [], [], []
    for i, shape in enumerate(shapes):
        explorer = TopExp_Explorer(_unwrap(shape), TopAbs_FACE)
        while explorer.More():
            face = explorer.Current()
            explorer.Next()
            orientation = face.Orientation()
            if orientation not in (TopAbs_FORWARD, TopAbs_REVERSED):
                # internal and external faces are not part of the boundary of the solid
                continue
            is_reversed = orientation == TopAbs_REVERSED
            k = face_map.Add(face) - 1
            if k == len(faces):
                faces.append(TopoDS.Face_s(face))
                first.append(i)
                second.append(NO_VOLUME)
                reversed_in_first.append(is_reversed)
            elif first[k] == i or second[k] == i:
                problems.append(f"solid {i + 1} uses one of its faces more than once")
            elif second[k] != NO_VOLUME:
                problems.append(
                    f"a face is shared by more than two solids: {first[k] + 1}, {second[k] + 1} and {i + 1}"
                )
            elif is_reversed == reversed_in_first[k]:
                problems.append(
                    f"solids {first[k] + 1} and {i + 1} lie on the same side of a face they share: they overlap"
                )
            else:
                second[k] = i
    return FaceTable(
        faces=faces,
        senses=np.array([first, second], dtype=np.int64).T.reshape(-1, 2),
        reversed_in_first=np.array(reversed_in_first, dtype=bool),
        problems=problems,
    )


def solid_size(shape) -> float:
    """Largest dimension of the bounding box of a shape, the size OpenCASCADE's mesher uses."""
    box = Bnd_Box()
    BRepBndLib.Add_s(_unwrap(shape), box, False)
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return max(xmax - xmin, ymax - ymin, zmax - zmin)


def _parameters(options: MeshOptions, n_solids: int, algorithm: Optional[str] = None) -> IMeshTools_Parameters:
    parameters = IMeshTools_Parameters()
    relative = options.tolerance_mode == "relative"
    deflection = options.tolerance
    if relative and n_solids > 1:
        deflection *= options.relative_factor
    parameters.Deflection = deflection
    parameters.Angle = options.angular_tolerance
    parameters.Relative = relative
    parameters.InParallel = options.threads > 1
    parameters.MeshAlgo = _ALGORITHMS[algorithm or options.algorithm]
    return parameters


def triangulate(shapes: Sequence, options: MeshOptions, algorithm: Optional[str] = None) -> None:
    """Triangulate the solids in place; OpenCASCADE stores the triangles on the faces.

    Existing triangulations are removed first. OpenCASCADE would otherwise
    keep any triangulation that is finer than requested, and the result would
    depend on what was done to the solids before.
    """
    shapes = [_unwrap(s) for s in shapes]
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    BRepTools.Clean_s(compound)
    with occ_threads(options.threads):
        if options.grouping == "model":
            BRepMesh_IncrementalMesh(compound, _parameters(options, len(shapes), algorithm))
        else:
            # Smallest first: in relative mode a smaller solid asks for a finer
            # division of a shared edge. The first division of an edge is then the
            # finest one, which OpenCASCADE keeps when it meets the edge again.
            for shape in sorted(shapes, key=solid_size):
                BRepMesh_IncrementalMesh(shape, _parameters(options, 1, algorithm))


def _matrix(transformation) -> np.ndarray:
    """The 3 x 4 matrix of a gp_Trsf."""
    return np.array([[transformation.Value(i, j) for j in range(1, 5)] for i in range(1, 4)])


def _dump_triangulations(triangulations) -> bytes:
    """Serialise triangulations through OpenCASCADE's binary shape format.

    The triangulations are attached to faces without geometry, so the dump
    holds nothing but the nodes and triangles, as raw doubles and integers.
    This is an order of magnitude faster than reading them node by node.
    """
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for triangulation in triangulations:
        face = TopoDS_Face()
        builder.MakeFace(face, triangulation)
        builder.Add(compound, face)
    stream = io.BytesIO()
    BinTools.Write_s(compound, stream)
    return stream.getvalue()


_BLOCK_HEADER = struct.Struct("<iibbd")  # nodes, triangles, has uv, has normals, deflection


def _parse_dump(raw: bytes, sizes):
    """Nodes, triangles and deflection of every triangulation in a binary dump.

    Returns None when the dump does not have the expected layout, in which
    case the triangulations have to be read through the API instead.
    """
    if not raw.lstrip().startswith(b"Open CASCADE Topology V4"):
        return None
    start = raw.find(b"\nTriangulations ")
    if start < 0:
        return None
    end = raw.index(b"\n", start + 1)
    if int(raw[start + 16:end]) != len(sizes):
        return None
    position = end + 1
    view = memoryview(raw)
    nodes, triangles, deflections = [], [], np.empty(len(sizes))
    for k, size in enumerate(sizes):
        n_nodes, n_triangles, has_uv, has_normals, deflections[k] = _BLOCK_HEADER.unpack_from(raw, position)
        if (n_nodes, n_triangles) != size:
            return None
        position += _BLOCK_HEADER.size
        nodes.append(np.frombuffer(view, "<f8", 3 * n_nodes, position))
        position += (24 + 16 * has_uv) * n_nodes
        triangles.append(np.frombuffer(view, "<i4", 3 * n_triangles, position))
        position += 12 * n_triangles + 12 * has_normals * n_nodes
    return nodes, triangles, deflections


def _read_through_api(triangulations):
    """Nodes, triangles and deflection of every triangulation, read node by node."""
    nodes = [
        np.array([t.Node(i).Coord() for i in range(1, t.NbNodes() + 1)], dtype=np.float64).ravel()
        for t in triangulations
    ]
    triangles = [
        np.array([t.Triangle(i).Get() for i in range(1, t.NbTriangles() + 1)], dtype=np.int32).ravel()
        for t in triangulations
    ]
    return nodes, triangles, np.array([t.Deflection() for t in triangulations])


@dataclass
class Triangulations:
    """The triangulations OpenCASCADE has stored on a list of faces, one face after the other."""

    #: (n, 3) nodes in global coordinates
    vertices: np.ndarray
    #: (m, 3) indices into vertices, in the node order of the triangulation
    triangles: np.ndarray
    #: face k owns triangles[offsets[k]:offsets[k + 1]]
    offsets: np.ndarray
    #: face k owns vertices[node_offsets[k]:node_offsets[k + 1]]
    node_offsets: np.ndarray
    #: True where the face is put in place by a location
    located: np.ndarray
    #: True where that location is a mirror transformation
    mirrored: np.ndarray
    #: largest deflection of the triangulations
    deflection: float
    #: indices of the faces that have no triangles
    missing: List[int]


def read_triangulations(faces, use_dump: bool = True) -> Triangulations:
    """Collect the triangulation OpenCASCADE has stored on every face."""
    location = TopLoc_Location()
    triangulations, owners, transforms, missing = [], [], {}, []
    for k, face in enumerate(faces):
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None or triangulation.NbTriangles() == 0:
            missing.append(k)
            continue
        if not location.IsIdentity():
            transforms[len(owners)] = _matrix(location.Transformation())
        owners.append(k)
        triangulations.append(triangulation)

    sizes = [(t.NbNodes(), t.NbTriangles()) for t in triangulations]
    parsed = _parse_dump(_dump_triangulations(triangulations), sizes) if use_dump and sizes else None
    nodes, tris, deflections = parsed if parsed is not None else _read_through_api(triangulations)

    n_nodes = np.array([s[0] for s in sizes], dtype=np.int64)
    n_tris = np.array([s[1] for s in sizes], dtype=np.int64)
    node_start = np.concatenate(([0], np.cumsum(n_nodes)))
    vertices = np.concatenate(nodes).reshape(-1, 3).astype(np.float64) if sizes else np.empty((0, 3))
    triangles = np.concatenate(tris).reshape(-1, 3).astype(np.int64) - 1 if sizes else np.empty((0, 3), np.int64)
    triangles += np.repeat(node_start[:-1], n_tris)[:, None]

    located = np.zeros(len(faces), dtype=bool)
    mirrored = np.zeros(len(faces), dtype=bool)
    for j, matrix in transforms.items():
        part = slice(node_start[j], node_start[j + 1])
        vertices[part] = vertices[part] @ matrix[:, :3].T + matrix[:, 3]
        located[owners[j]] = True
        mirrored[owners[j]] = np.linalg.det(matrix[:, :3]) < 0

    counts = np.zeros((2, len(faces)), dtype=np.int64)
    counts[0, owners] = n_nodes
    counts[1, owners] = n_tris
    return Triangulations(
        vertices=vertices,
        triangles=triangles,
        offsets=np.concatenate(([0], np.cumsum(counts[1]))),
        node_offsets=np.concatenate(([0], np.cumsum(counts[0]))),
        located=located,
        mirrored=mirrored,
        deflection=float(np.max(deflections)) if len(sizes) else 0.0,
        missing=missing,
    )


def _same_point_pairs(faces, mesh: Triangulations, candidates, tolerance: float):
    """Pairs of nodes that are one and the same point of an edge in the triangulations of two faces.

    OpenCASCADE divides an edge once and records, for every face along the
    edge, which nodes of the triangulation of that face lie on it, in the order
    of the edge. Nodes at the same place in these lists are the same point,
    even when their coordinates differ in the last digits, as they do for faces
    that are put in place by different locations.
    """
    edges = TopTools_IndexedMapOfShape()
    first, pairs = [], []
    location = TopLoc_Location()
    for k in candidates:
        face = faces[k]
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        explorer = TopExp_Explorer(face, TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            explorer.Next()
            polygon = BRep_Tool.PolygonOnTriangulation_s(edge, triangulation, location)
            if polygon is None:
                continue
            nodes = np.fromiter(polygon.Nodes(), dtype=np.int64, count=polygon.NbNodes())
            nodes += mesh.node_offsets[k] - 1
            i = edges.Add(edge) - 1
            if i == len(first):
                first.append(nodes)
                continue
            reference = first[i]
            if len(reference) != len(nodes):
                continue
            # the lists run in the same direction; the distance guards against any exception to that
            for candidate in (nodes, nodes[::-1]):
                apart = np.abs(mesh.vertices[reference] - mesh.vertices[candidate]).max()
                if apart <= tolerance:
                    pairs.append(np.stack([reference, candidate]))
                    break
    if not pairs:
        return np.empty((2, 0), dtype=np.int64)
    return np.concatenate(pairs, axis=1)


def _join(index: np.ndarray, a: np.ndarray, b: np.ndarray):
    """Give the vertices a[i] and b[i] one number, for every i.

    Parameters
    ----------
    index : (n,) int array
        Number of the vertex every node maps to so far.
    a, b : (m,) int arrays
        Numbers of vertices that are the same point.

    Returns
    -------
    index : (n,) int array
        The new vertex number of every node, without gaps.
    n_vertices : int
    """
    involved, local = np.unique(np.concatenate([a, b]), return_inverse=True)
    a, b = local[: len(a)], local[len(a):]
    root = np.arange(len(involved))
    while True:
        ra, rb = root[a], root[b]
        differ = ra != rb
        if not differ.any():
            break
        # hang the group with the higher number below the one with the lower number
        np.minimum.at(root, np.maximum(ra, rb)[differ], np.minimum(ra, rb)[differ])
        while True:
            shorter = root[root]
            if np.array_equal(shorter, root):
                break
            root = shorter
    n_before = int(index.max()) + 1 if len(index) else 0
    target = np.arange(n_before)
    target[involved] = involved[root]
    kept = np.zeros(n_before, dtype=bool)
    kept[target] = True
    renumbered = np.cumsum(kept) - 1
    return renumbered[target[index]], int(kept.sum())


def _largest_vertex_tolerance(shapes) -> float:
    """Largest tolerance of the vertices of some shapes: the accuracy the CAD model claims for them."""
    vertices = TopTools_IndexedMapOfShape()
    for shape in shapes:
        TopExp.MapShapes_s(shape, TopAbs_VERTEX, vertices)
    return max(
        (BRep_Tool.Tolerance_s(TopoDS.Vertex_s(vertices.FindKey(i))) for i in range(1, vertices.Extent() + 1)),
        default=0.0,
    )


def _open_vertices_within(model: MeshedModel, report, limit: float, most: int = 20000):
    """Pairs of vertices on the open edges of a volume that are at most `limit` apart.

    Returns
    -------
    a, b : int arrays
        Vertex numbers of the pairs.
    apart : float
        Largest distance within a pair.
    """
    edges = report.open_edge_vertices
    first, second, apart = [], [], 0.0
    for volume in np.unique(edges[:, 0]):
        vertices = np.unique(edges[edges[:, 0] == volume, 1:])
        if len(vertices) > most:
            # this many open edges are not a matter of vertices that almost coincide
            continue
        points = model.vertices[vertices]
        rows = max(1, 2_000_000 // len(points))
        for start in range(0, len(points), rows):
            distance = np.linalg.norm(points[start:start + rows, None, :] - points[None, :, :], axis=2)
            i, j = np.nonzero(distance <= limit)
            close = i + start < j
            if close.any():
                first.append(vertices[i[close] + start])
                second.append(vertices[j[close]])
                apart = max(apart, float(distance[i[close], j[close]].max()))
    if not first:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64), 0.0
    return np.concatenate(first), np.concatenate(second), apart


def _describe(indices, names, limit=8) -> str:
    indices = sorted(set(int(i) for i in indices))
    parts = [f"{i + 1}" + (f" ('{names[i]}')" if names and names[i] else "") for i in indices[:limit]]
    return ", ".join(parts) + (f", ... ({len(indices)} in total)" if len(indices) > limit else "")


def _build_model(mesh: Triangulations, index, n_vertices, senses, tags, names, implicit_complement):
    """The model that results when the nodes of the triangulations are given common vertices."""
    vertices = np.empty((n_vertices, 3), dtype=np.float64)
    vertices[index] = mesh.vertices
    triangles = index[mesh.triangles]
    offsets = mesh.offsets
    # triangles that collapse to a line, as they occur at the apex of a cone or the pole of a sphere
    proper = (
        (triangles[:, 0] != triangles[:, 1])
        & (triangles[:, 1] != triangles[:, 2])
        & (triangles[:, 0] != triangles[:, 2])
    )
    n_collapsed = int(len(proper) - np.count_nonzero(proper))
    if n_collapsed:
        kept = np.concatenate(([0], np.cumsum(proper)))
        offsets = kept[offsets]
        triangles = triangles[proper]
    # a face of which nothing is left, such as a degenerate face, is not a surface of the model
    filled = np.diff(offsets) > 0
    if not filled.all():
        offsets = np.concatenate(([0], offsets[1:][filled]))
        senses = senses[filled]
    model = MeshedModel(
        vertices=vertices,
        triangles=triangles,
        surface_offsets=offsets,
        surface_senses=senses,
        volume_tags=list(tags),
        volume_names=list(names) if names is not None else None,
        implicit_complement=implicit_complement,
        faceting_tolerance=mesh.deflection,
    )
    return model, n_collapsed, int(len(filled) - np.count_nonzero(filled))


def mesh_and_check(
    shapes: Sequence,
    tags: Sequence[str],
    options: MeshOptions,
    names: Optional[Sequence[str]] = None,
    implicit_complement: Optional[str] = None,
    cad_volumes: Optional[Sequence[float]] = None,
    volume_rtol: Optional[float] = None,
):
    """Triangulate solids, and check that the surface mesh encloses every one of them.

    Parameters
    ----------
    shapes : sequence of cadquery solids or TopoDS_Shape
        One per volume. Solids that touch must share their common faces
        topologically (see Assembly.merge_all) for the face to be shared.
    tags : sequence of str
        Material tag of every solid.
    options : MeshOptions
    names : sequence of str, optional
        Name of the CAD part of every solid, used in messages.
    implicit_complement : str, optional
        Material tag of the space around the solids.
    cad_volumes, volume_rtol
        Passed on to validate.check_model.

    Returns
    -------
    model : MeshedModel
    report : validate.ValidationReport
        The outcome of the checks. Volumes that are not closed are reported
        here, they do not raise an exception.

    Raises
    ------
    MeshingError
        In strict mode, when solids overlap or a face could not be triangulated.
    """
    shapes = [_unwrap(s) for s in shapes]
    seconds = {}
    start = time.perf_counter()
    table = collect_faces(shapes)
    seconds["topology"] = time.perf_counter() - start
    if table.problems and options.strict:
        raise MeshingError(
            "The solids do not form a valid assembly:\n  " + "\n  ".join(table.problems[:20])
            + "\nRemove the overlap in the CAD model, or set mesher_config['strict'] = False to mesh anyway."
        )

    start = time.perf_counter()
    triangulate(shapes, options)
    mesh = read_triangulations(table.faces)
    algorithm = options.algorithm
    if mesh.missing:
        # give the other triangulation algorithm a chance before giving up
        other = "delabella" if options.algorithm == "watson" else "watson"
        triangulate(shapes, options, algorithm=other)
        retry = read_triangulations(table.faces)
        if len(retry.missing) < len(mesh.missing):
            mesh, algorithm = retry, other
        else:
            # what is stored on the faces has to remain what was read from them
            triangulate(shapes, options)
            mesh = read_triangulations(table.faces)
    seconds["mesh"] = time.perf_counter() - start
    if mesh.missing:
        solids = table.senses[mesh.missing].ravel()
        n = len(mesh.missing)
        message = (
            f"{n} face{'s' if n > 1 else ''} could not be triangulated, in solid(s) "
            f"{_describe(solids[solids != NO_VOLUME], names)}."
        )
        if options.strict:
            raise MeshingError(
                message + " The geometry would leak particles. Repair these parts in the CAD model, "
                "or set mesher_config['strict'] = False to write the geometry anyway."
            )

    start = time.perf_counter()
    # triangles have to point out of the first solid of their face
    flip = np.repeat(table.reversed_in_first ^ mesh.mirrored, np.diff(mesh.offsets))
    mesh.triangles[flip] = mesh.triangles[flip][:, ::-1]
    index, n_vertices = weld_vertices(mesh.vertices)
    n_exact = n_vertices
    # the same point comes back from two faces with rounding errors at most
    tolerance = 1e-6 * float(np.abs(mesh.vertices).max()) if len(mesh.vertices) else 0.0
    joined = np.zeros(len(table.faces), dtype=bool)

    def join_faces_of(volumes) -> bool:
        """Join the faces of some volumes along their edges; True if that changed anything."""
        nonlocal index, n_vertices
        volumes = np.asarray(volumes)
        faces = np.flatnonzero(np.isin(table.senses, volumes[volumes != NO_VOLUME]).any(axis=1) & ~joined)
        joined[faces] = True
        pairs = _same_point_pairs(table.faces, mesh, faces, tolerance)
        before = n_vertices
        if pairs.shape[1]:
            index, n_vertices = _join(index, index[pairs[0]], index[pairs[1]])
        return n_vertices != before

    def build_and_check():
        built = _build_model(mesh, index, n_vertices, table.senses, tags, names, implicit_complement)
        return built, check_model(built[0], cad_volumes=cad_volumes, volume_rtol=volume_rtol)

    # A face that is put in place by a location comes back with coordinates that
    # differ in the last digits from those of its neighbours.
    if mesh.located.any():
        join_faces_of(np.unique(table.senses[mesh.located]))
    seconds["weld"] = time.perf_counter() - start

    start = time.perf_counter()
    built, report = build_and_check()
    if report.leaky_volumes and join_faces_of([v.index for v in report.leaky_volumes]):
        built, report = build_and_check()
    n_along_edges = n_exact - n_vertices
    largest_gap = 0.0
    if report.leaky_volumes:
        # Edges that coincide without being one and the same edge of the CAD model
        # end in vertices that coincide within the tolerance of those vertices only.
        limit = 2.0 * _largest_vertex_tolerance([shapes[v.index] for v in report.leaky_volumes])
        a, b, largest_gap = _open_vertices_within(built[0], report, limit)
        if len(a):
            index, n_vertices = _join(index, a, b)
            built, report = build_and_check()
    seconds["check"] = time.perf_counter() - start

    model, n_collapsed, n_empty = built
    model.info = {
        "algorithm": algorithm,
        "tolerance_mode": options.tolerance_mode,
        "threads": options.threads,
        "seconds": seconds,
        "untriangulated_faces": len(mesh.missing),
        "collapsed_triangles": n_collapsed,
        "empty_faces": n_empty,
        "located_faces": int(np.count_nonzero(mesh.located)),
        "vertices_joined_along_edges": n_along_edges,
        "vertices_joined_across_gaps": n_exact - n_along_edges - n_vertices,
        "largest_gap_joined": largest_gap,
        "topology_problems": list(table.problems),
    }
    return model, report
