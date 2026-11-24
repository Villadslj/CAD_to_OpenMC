"""Independent checks of a surface-meshed geometry.

The checks only look at vertices, triangles and surface senses. They do not
know how the mesh was produced, so they can judge every meshing backend, and
also h5m-files written by other tools:

    python -m CAD_to_OpenMC.validate geometry.h5m

For transport to work every volume must be bounded by a closed surface mesh:
each edge of its triangles has to be used by exactly two triangles, once in
each direction. These are reported as errors.

Warnings are given for what does not stop transport but deserves a look: two
surfaces that hold the same triangles, which means two volumes touch without
sharing their common boundary, and volumes whose mesh differs much in volume
from the CAD solid.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from .meshed_model import MeshedModel, NO_VOLUME


class MeshValidationError(RuntimeError):
    """A meshed geometry is not fit for particle transport.

    The full ValidationReport is available as the attribute `report`.
    """

    def __init__(self, message: str, report: Optional["ValidationReport"] = None):
        super().__init__(message)
        self.report = report


@dataclass
class VolumeCheck:
    index: int
    id: int
    tag: str
    name: Optional[str]
    n_surfaces: int
    n_triangles: int
    open_edges: int
    nonmanifold_edges: int
    misoriented_edges: int
    degenerate_triangles: int
    mesh_volume: float
    cad_volume: Optional[float] = None
    #: coordinates of a vertex of one of the open edges, to find the place in the CAD model
    open_edge_at: Optional[tuple] = None

    @property
    def closed(self) -> bool:
        return self.n_triangles > 0 and self.open_edges == 0 and self.nonmanifold_edges == 0

    @property
    def oriented(self) -> bool:
        return self.misoriented_edges == 0 and self.mesh_volume > 0

    @property
    def volume_error(self) -> Optional[float]:
        """Relative difference between the mesh volume and the CAD volume."""
        if self.cad_volume is None or self.cad_volume == 0:
            return None
        return (self.mesh_volume - self.cad_volume) / self.cad_volume

    def label(self) -> str:
        name = f", part '{self.name}'" if self.name else ""
        return f"volume {self.id} (material '{self.tag}'{name})"


@dataclass
class ValidationReport:
    volumes: List[VolumeCheck]
    #: number of triangles that also occur in another surface
    duplicate_triangles: int = 0
    #: (surface id, surface id, number of common triangles)
    duplicate_surface_pairs: List[tuple] = field(default_factory=list)
    #: number of triangles that occur more than once within their own surface
    repeated_triangles: int = 0
    #: ids of surfaces without a forward volume, or with the same volume on both sides
    bad_sense_surfaces: List[int] = field(default_factory=list)
    #: ids of surfaces without triangles
    empty_surfaces: List[int] = field(default_factory=list)
    #: relative volume error above which a volume is reported, None to report none
    volume_rtol: Optional[float] = None
    #: (n, 3) int array [index of the volume, vertex, vertex] of every open edge;
    #: the vertices are numbered as in the model that was checked
    open_edge_vertices: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def leaky_volumes(self) -> List[VolumeCheck]:
        return [v for v in self.volumes if not v.closed]

    @property
    def misoriented_volumes(self) -> List[VolumeCheck]:
        return [v for v in self.volumes if v.closed and not v.oriented]

    def errors(self) -> List[str]:
        """One line for everything that makes the geometry unfit for transport."""
        out = []
        for v in self.volumes:
            if v.n_triangles == 0:
                out.append(f"{v.label()} has no triangles")
                continue
            if v.open_edges or v.nonmanifold_edges:
                where = ""
                if v.open_edge_at is not None:
                    where = ", for instance at (" + ", ".join(f"{x:.6g}" for x in v.open_edge_at) + ")"
                out.append(
                    f"{v.label()} is not closed: {v.open_edges} open and "
                    f"{v.nonmanifold_edges} non-manifold edges{where}"
                )
            if v.misoriented_edges:
                out.append(f"{v.label()} has {v.misoriented_edges} edges with inconsistent triangle orientation")
            elif v.closed and v.mesh_volume <= 0:
                out.append(f"{v.label()} is inside out: mesh volume {v.mesh_volume:.6g}")
        if self.repeated_triangles:
            out.append(f"{self.repeated_triangles} triangles are repeated within their surface")
        if self.bad_sense_surfaces:
            out.append(f"surfaces without a valid pair of volumes: {_short_list(self.bad_sense_surfaces)}")
        return out

    def warnings(self) -> List[str]:
        """One line for everything that does not stop transport but may give wrong results."""
        out = []
        for v in self.volumes:
            err = v.volume_error
            if self.volume_rtol is not None and err is not None and abs(err) > self.volume_rtol:
                out.append(
                    f"{v.label()} has a mesh volume of {v.mesh_volume:.6g} but a CAD volume of "
                    f"{v.cad_volume:.6g} ({100 * err:+.2f} %)"
                )
        for s1, s2, n in self.duplicate_surface_pairs:
            out.append(f"surfaces {s1} and {s2} hold {n} identical triangles: coincident surfaces that are not shared")
        if self.empty_surfaces:
            out.append(f"surfaces without triangles: {_short_list(self.empty_surfaces)}")
        return out

    def problems(self) -> List[str]:
        """The errors followed by the warnings."""
        return self.errors() + self.warnings()

    @property
    def fit_for_transport(self) -> bool:
        """True when there are no errors: every volume is closed and consistently oriented."""
        return not self.errors()

    @property
    def ok(self) -> bool:
        """True when there are neither errors nor warnings."""
        return not self.problems()

    def summary(self, limit: Optional[int] = None) -> str:
        """The outcome in a few lines, with at most `limit` errors and `limit` warnings."""
        n = len(self.volumes)
        closed = sum(v.closed for v in self.volumes)
        lines = [f"{closed} of {n} volumes are closed"]
        deviations = [abs(v.volume_error) for v in self.volumes if v.volume_error is not None]
        if deviations:
            lines.append(f"largest difference between mesh and CAD volume: {100 * max(deviations):.3g} %")
        for kind, items in (("errors", self.errors()), ("warnings", self.warnings())):
            if not items:
                continue
            lines.append(f"{len(items)} {kind}:")
            shown = items if limit is None else items[:limit]
            lines += ["  " + p for p in shown]
            if len(shown) < len(items):
                lines.append(f"  ... and {len(items) - len(shown)} more")
        if len(lines) == 1 + bool(deviations):
            lines.append("no problems found")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = asdict(self)
        for v, src in zip(d["volumes"], self.volumes):
            v["closed"] = src.closed
            v["oriented"] = src.oriented
            v["volume_error"] = src.volume_error
        if self.open_edge_vertices is not None:
            d["open_edge_vertices"] = self.open_edge_vertices.tolist()
        d["ok"] = self.ok
        d["fit_for_transport"] = self.fit_for_transport
        d["errors"] = self.errors()
        d["warnings"] = self.warnings()
        return d


def _short_list(items, limit=12) -> str:
    items = list(items)
    text = ", ".join(str(i) for i in items[:limit])
    return text + (f", ... ({len(items)} in total)" if len(items) > limit else "")


def weld_vertices(vertices: np.ndarray):
    """Give vertices with identical coordinates a common index.

    Returns
    -------
    index : (n_vertices,) int array
        Index of the welded vertex each vertex maps to.
    n_welded : int
    """
    n = len(vertices)
    if n == 0:
        return np.empty(0, dtype=np.int64), 0
    order = np.lexsort((vertices[:, 2], vertices[:, 1], vertices[:, 0]))
    ordered = vertices[order]
    new = np.ones(n, dtype=bool)
    new[1:] = np.any(ordered[1:] != ordered[:-1], axis=1)
    index = np.empty(n, dtype=np.int64)
    index[order] = np.cumsum(new) - 1
    return index, int(new.sum())


def _run_starts(*sorted_keys) -> np.ndarray:
    """Start index of every run of equal rows in keys that are sorted together."""
    n = len(sorted_keys[0])
    first = np.zeros(n, dtype=bool)
    if n:
        first[0] = True
    for key in sorted_keys:
        first[1:] |= key[1:] != key[:-1]
    return np.flatnonzero(first)


def check_model(
    model: MeshedModel,
    cad_volumes: Optional[Sequence[float]] = None,
    volume_rtol: Optional[float] = None,
) -> ValidationReport:
    """Check that a meshed model is fit for particle transport.

    Parameters
    ----------
    model : MeshedModel
    cad_volumes : sequence of float, optional
        Volume of every CAD solid, to compare the mesh volumes with.
    volume_rtol : float, optional
        Report volumes whose mesh volume differs from the CAD volume by more
        than this fraction.
    """
    n_vol = model.n_volumes
    index, n_welded = weld_vertices(model.vertices)
    welded = index[model.triangles]
    tri_surface = model.triangle_surfaces()
    forward = model.surface_senses[tri_surface, 0] if model.n_triangles else np.empty(0, dtype=np.int64)
    reverse = model.surface_senses[tri_surface, 1] if model.n_triangles else np.empty(0, dtype=np.int64)
    has_f = forward != NO_VOLUME
    has_r = reverse != NO_VOLUME

    # every triangle once for each volume it bounds, oriented out of that volume
    tris = np.concatenate([welded[has_f], welded[has_r][:, ::-1]])
    raw = np.concatenate([model.triangles[has_f], model.triangles[has_r][:, ::-1]])
    vol = np.concatenate([forward[has_f], reverse[has_r]])

    degenerate = (tris[:, 0] == tris[:, 1]) | (tris[:, 1] == tris[:, 2]) | (tris[:, 0] == tris[:, 2])
    n_triangles = np.bincount(vol, minlength=n_vol)
    n_degenerate = np.bincount(vol[degenerate], minlength=n_vol)

    if model.n_vertices:
        lower, upper = model.vertices.min(axis=0), model.vertices.max(axis=0)
        points = model.vertices - 0.5 * (lower + upper)
    else:
        points = model.vertices
    six_volumes = np.einsum("ij,ij->i", points[raw[:, 0]], np.cross(points[raw[:, 1]], points[raw[:, 2]]))
    mesh_volume = np.bincount(vol, weights=six_volumes, minlength=n_vol) / 6.0

    # edges of every volume: a closed, consistently oriented mesh uses every
    # edge exactly twice, once in each direction
    good = tris[~degenerate]
    a = good.ravel()
    b = good[:, [1, 2, 0]].ravel()
    edge_vol = np.repeat(vol[~degenerate], 3)
    low, high = np.minimum(a, b), np.maximum(a, b)
    direction = np.where(a < b, 1, -1)
    order = np.lexsort((high, low, edge_vol))
    edge_vol, low, high, direction = edge_vol[order], low[order], high[order], direction[order]
    starts = _run_starts(edge_vol, low, high)
    if len(starts):
        uses = np.diff(np.append(starts, len(order)))
        balance = np.add.reduceat(direction, starts)
        owner = edge_vol[starts]
    else:
        uses = balance = owner = np.empty(0, dtype=np.int64)
    open_edges = np.bincount(owner[uses == 1], minlength=n_vol)
    nonmanifold = np.bincount(owner[uses > 2], minlength=n_vol)
    misoriented = np.bincount(owner[(uses == 2) & (balance != 0)], minlength=n_vol)

    # the open edges in terms of the vertices of the model, and one place per volume to look for them
    is_open = starts[uses == 1] if len(starts) else np.empty(0, dtype=np.int64)
    representative = np.empty(n_welded, dtype=np.int64)
    representative[index] = np.arange(model.n_vertices)
    open_edge_vertices = np.stack(
        [edge_vol[is_open], representative[low[is_open]], representative[high[is_open]]], axis=1
    )
    leaky, first = np.unique(open_edge_vertices[:, 0], return_index=True)
    open_edge_at = {
        int(v): tuple(float(x) for x in model.vertices[open_edge_vertices[i, 1]]) for v, i in zip(leaky, first)
    }

    n_surfaces = np.zeros(n_vol, dtype=np.int64)
    senses = model.surface_senses
    for column in (0, 1):
        used = senses[:, column][senses[:, column] != NO_VOLUME]
        n_surfaces += np.bincount(used, minlength=n_vol)

    volumes = [
        VolumeCheck(
            index=v,
            id=int(model.volume_ids[v]),
            tag=model.volume_tags[v],
            name=model.volume_names[v] if model.volume_names is not None else None,
            n_surfaces=int(n_surfaces[v]),
            n_triangles=int(n_triangles[v]),
            open_edges=int(open_edges[v]),
            nonmanifold_edges=int(nonmanifold[v]),
            misoriented_edges=int(misoriented[v]),
            degenerate_triangles=int(n_degenerate[v]),
            mesh_volume=float(mesh_volume[v]),
            cad_volume=float(cad_volumes[v]) if cad_volumes is not None else None,
            open_edge_at=open_edge_at.get(v),
        )
        for v in range(n_vol)
    ]
    report = ValidationReport(volumes=volumes, volume_rtol=volume_rtol, open_edge_vertices=open_edge_vertices)

    # identical triangles: within a surface, or in two different surfaces
    proper = (welded[:, 0] != welded[:, 1]) & (welded[:, 1] != welded[:, 2]) & (welded[:, 0] != welded[:, 2])
    keys = np.sort(welded[proper], axis=1)
    key_surface = tri_surface[proper]
    order = np.lexsort((key_surface, keys[:, 2], keys[:, 1], keys[:, 0]))
    keys, key_surface = keys[order], key_surface[order]
    same = np.flatnonzero(np.all(keys[1:] == keys[:-1], axis=1)) if len(keys) > 1 else np.empty(0, dtype=np.int64)
    if len(same):
        s1, s2 = key_surface[same], key_surface[same + 1]
        across = s1 != s2
        report.repeated_triangles = int(np.count_nonzero(~across))
        report.duplicate_triangles = int(np.count_nonzero(across))
        if report.duplicate_triangles:
            pairs, counts = np.unique(np.stack([s1[across], s2[across]], axis=1), axis=0, return_counts=True)
            report.duplicate_surface_pairs = [
                (int(model.surface_ids[p]), int(model.surface_ids[q]), int(n)) for (p, q), n in zip(pairs, counts)
            ]

    bad = (senses[:, 0] == NO_VOLUME) | (senses[:, 0] == senses[:, 1])
    report.bad_sense_surfaces = [int(i) for i in model.surface_ids[bad]]
    report.empty_surfaces = [int(i) for i in model.surface_ids[np.diff(model.surface_offsets) == 0]]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m CAD_to_OpenMC.validate",
        description="Check that the volumes in a DAGMC h5m-file are closed and consistently oriented. "
        "The exit status is 1 when the geometry is not fit for transport.",
    )
    parser.add_argument("h5m_file")
    parser.add_argument("--json", metavar="FILE", help="also write the full report to FILE")
    args = parser.parse_args(argv)

    from .h5m_io import read_h5m

    model = read_h5m(args.h5m_file)
    report = check_model(model)
    print(f"{args.h5m_file}: {model.summary()}")
    print(report.summary())
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report.to_dict(), f, indent=1)
    return 0 if report.fit_for_transport else 1


if __name__ == "__main__":
    sys.exit(main())
