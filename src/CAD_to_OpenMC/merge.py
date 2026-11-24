"""Making solids that touch share their common faces, and finding solids that overlap.

Where two solids touch, each of them has a face of its own. For particle
transport that has to be one face that both solids know of: two separate faces
get triangulated separately, and the volumes then overlap or leave gaps between
them. OpenCASCADE replaces faces that coincide by a shared one, and records
for every solid it was given which solids of the result stem from it. That
record is used here to tell which solid of the result takes the place of which
part, instead of guessing it from volumes and positions.

connect_solids does this the way it was always done: only faces, edges and
vertices that coincide are looked for (gluing). That is robust for parts that
touch, also when the contact is not exact in the CAD model, but a part that
sticks into another one goes unnoticed unless faces of the two happen to
coincide. find_overlaps computes all intersections instead, which finds every
overlap, at about twice the cost.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

from OCP.BOPAlgo import BOPAlgo_Builder, BOPAlgo_GlueOff, BOPAlgo_GlueShift
from OCP.BRep import BRep_Builder
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_FACE, TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape, TopoDS_Shell, TopoDS_Solid
from OCP.TopTools import TopTools_IndexedMapOfShape


class MergeError(RuntimeError):
    """OpenCASCADE could not process the solids."""


@dataclass
class Overlap:
    """A region that more than one solid lays claim to."""

    #: indices of the solids that overlap
    solids: Tuple[int, ...]
    #: volume of the region they have in common
    volume: float
    #: index of the solid that takes precedence in the region: the smallest one
    owner: int


@dataclass
class ConnectedSolids:
    """The outcome of connect_solids."""

    #: for every solid that was given, the solids that take its place. Usually
    #: one. Empty if the solid lies completely inside solids that take
    #: precedence, several if such solids cut it into parts that do not touch.
    pieces: List[List[TopoDS_Shape]]
    overlaps: List[Overlap] = field(default_factory=list)
    #: what OpenCASCADE had to remark
    warnings: List[str] = field(default_factory=list)


def _unwrap(shape):
    """The TopoDS_Shape of a cadquery object, or the object itself."""
    return getattr(shape, "wrapped", shape)


def _solids(shape: TopoDS_Shape) -> TopTools_IndexedMapOfShape:
    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    return solids


def _volume(shape: TopoDS_Shape) -> float:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return properties.Mass()


def _messages(dump) -> List[str]:
    """The lines OpenCASCADE writes with DumpErrors or DumpWarnings."""
    stream = io.BytesIO()
    dump(stream)
    return [line.strip() for line in stream.getvalue().decode(errors="replace").splitlines() if line.strip()]


def _unite(pieces: List[TopoDS_Shape]) -> List[TopoDS_Shape]:
    """Join solids that share faces into one solid, without the faces between them.

    The solids that come out are bounded by the very faces of the solids that
    went in, so they still share those with their neighbours. Solids that do
    not touch any of the others through a face are returned as they are.
    """
    faces = TopTools_IndexedMapOfShape()
    used_by: List[List[tuple]] = []
    for p, piece in enumerate(pieces):
        explorer = TopExp_Explorer(piece, TopAbs_FACE)
        while explorer.More():
            face = explorer.Current()
            explorer.Next()
            if face.Orientation() not in (TopAbs_FORWARD, TopAbs_REVERSED):
                continue
            k = faces.Add(face) - 1
            if k == len(used_by):
                used_by.append([])
            used_by[k].append((p, face))

    # pieces that share a face belong together
    group = list(range(len(pieces)))

    def root(p):
        while group[p] != p:
            group[p] = group[group[p]]
            p = group[p]
        return p

    for users in used_by:
        for p, _ in users[1:]:
            a, b = root(users[0][0]), root(p)
            group[max(a, b)] = min(a, b)

    united = []
    builder = BRep_Builder()
    for first in sorted({root(p) for p in range(len(pieces))}):
        members = {p for p in range(len(pieces)) if root(p) == first}
        if len(members) == 1:
            united.append(pieces[first])
            continue
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        for users in used_by:
            # a face between two of the pieces lies inside the solid they form together
            if len(users) == 1 and users[0][0] in members:
                builder.Add(shell, users[0][1])
        shell.Closed(True)
        solid = TopoDS_Solid()
        builder.MakeSolid(solid)
        builder.Add(solid, shell)
        united.append(solid)
    return united


def _fuse(shapes: List[TopoDS_Shape], all_intersections: bool):
    """Let OpenCASCADE cut the solids along each other's faces.

    Returns
    -------
    builder : BOPAlgo_Builder
    result : TopTools_IndexedMapOfShape
        The solids of the result.
    origins : list of list of int
        For every solid of the result, the indices of the given solids it stems from.
    """
    builder = BOPAlgo_Builder()
    for shape in shapes:
        builder.AddArgument(shape)
    builder.SetGlue(BOPAlgo_GlueOff if all_intersections else BOPAlgo_GlueShift)
    # the given solids are left as they are
    builder.SetNonDestructive(True)
    # Running this in parallel gains little: most of the work is not divided over threads.
    builder.SetRunParallel(False)
    builder.Perform()
    if builder.HasErrors() or builder.Shape().IsNull():
        raise MergeError(
            "OpenCASCADE could not process the solids: "
            + ("; ".join(_messages(builder.DumpErrors)) or "no result")
            + ". Check the CAD model for invalid solids."
        )
    result = _solids(builder.Shape())
    origins: List[List[int]] = [[] for _ in range(result.Extent())]
    for i, shape in enumerate(shapes):
        images = builder.Modified(shape)
        # a solid that needed no change is part of the result as it is
        for image in (images if images.Size() else [shape]):
            found = _solids(image)
            for k in range(1, found.Extent() + 1):
                index = result.FindIndex(found.FindKey(k))
                if index and i not in origins[index - 1]:
                    origins[index - 1].append(i)
    return builder, result, origins


def _claims(shapes, result, origins):
    """Decide which of the given solids every solid of the result belongs to.

    Returns
    -------
    owners : list of int or None
        For every solid of the result the index of the given solid it belongs to.
    overlaps : list of Overlap
        The solids of the result that stem from more than one given solid.
    """
    volumes = {}

    def size(i):
        if i not in volumes:
            volumes[i] = _volume(shapes[i])
        return volumes[i], i

    owners, overlaps = [], []
    for index, claimed_by in enumerate(origins, start=1):
        if len(claimed_by) > 1:
            # the smallest solid, and of solids of equal volume the first one
            owners.append(min(claimed_by, key=size))
            region = _volume(result.FindKey(index))
            overlaps.append(Overlap(solids=tuple(claimed_by), volume=region, owner=owners[-1]))
        else:
            owners.append(claimed_by[0] if claimed_by else None)
    return owners, overlaps


def connect_solids(solids: Sequence) -> ConnectedSolids:
    """Make solids that touch share their common faces.

    Only faces, edges and vertices that coincide are looked for. Solids that
    overlap are told apart where that brings an overlap to light, which is the
    case when faces of the two coincide, as for a part that is embedded in
    another one. The region they have in common is then given to the smallest
    of them, and of solids of equal volume to the one that comes first. Nothing
    is left out: the solids that are returned fill the same space as the solids
    that were given. Use find_overlaps to check for all overlaps.

    Parameters
    ----------
    solids : sequence of cadquery solids or TopoDS_Shape
        They are not changed.

    Raises
    ------
    MergeError
        If OpenCASCADE reports that it could not process the solids.
    """
    shapes = [_unwrap(s) for s in solids]
    if len(shapes) < 2:
        return ConnectedSolids(pieces=[[shape] for shape in shapes])
    builder, result, origins = _fuse(shapes, all_intersections=False)
    owners, overlaps = _claims(shapes, result, origins)
    connected = ConnectedSolids(pieces=[[] for _ in shapes], overlaps=overlaps)
    if builder.HasWarnings():
        connected.warnings = _messages(builder.DumpWarnings)
    for index, owner in enumerate(owners, start=1):
        if owner is not None:
            connected.pieces[owner].append(result.FindKey(index))
    # a solid that keeps a region it shares with another one has that region as a piece of its own
    connected.pieces = [_unite(owned) if len(owned) > 1 else owned for owned in connected.pieces]
    return connected


def find_overlaps(solids: Sequence) -> List[Overlap]:
    """Find the regions that more than one solid lays claim to.

    All intersections between the solids are computed, which takes about twice
    as long as connect_solids. Contacts that are not exact in the CAD model
    show up as overlaps of very small volume. The solids are not changed.

    Returns
    -------
    list of Overlap
        The largest first.

    Raises
    ------
    MergeError
        If OpenCASCADE reports that it could not process the solids.
    """
    shapes = [_unwrap(s) for s in solids]
    if len(shapes) < 2:
        return []
    _, result, origins = _fuse(shapes, all_intersections=True)
    return sorted(_claims(shapes, result, origins)[1], key=lambda overlap: -overlap.volume)
