"""In-memory representation of a surface-meshed DAGMC geometry.

A MeshedModel holds what ends up in a h5m-file: a list of vertices, the
triangles of every surface, and for every surface the two volumes it separates.
All meshing backends produce this structure, and the validators and writers
work on it, which is why no intermediate files are needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

#: value used in surface_senses when there is no volume on that side
NO_VOLUME = -1


@dataclass
class MeshedModel:
    """Surface mesh of a set of volumes.

    Parameters
    ----------
    vertices : (n_vertices, 3) float array
        Coordinates. A vertex may be shared by any number of surfaces.
    triangles : (n_triangles, 3) int array
        Vertex indices. The triangles of one surface are stored contiguously.
    surface_offsets : (n_surfaces + 1,) int array
        Surface s owns triangles[surface_offsets[s]:surface_offsets[s + 1]].
    surface_senses : (n_surfaces, 2) int array
        [forward volume, reverse volume] as volume indices. The triangle normals
        (right hand rule) point out of the forward volume and into the reverse
        volume. NO_VOLUME marks a missing volume.
    volume_tags : list of str
        Material tag of every volume.
    volume_names : list of str, optional
        Name of the CAD part each volume stems from.
    implicit_complement : str, optional
        Material tag of the space not claimed by any volume.
    faceting_tolerance : float, optional
        Largest distance between the mesh and the CAD surface, in the units of
        the vertices.
    volume_ids, surface_ids : int arrays, optional
        The ids OpenMC/DAGMC will see. They default to 1..n.
    """

    vertices: np.ndarray
    triangles: np.ndarray
    surface_offsets: np.ndarray
    surface_senses: np.ndarray
    volume_tags: List[str]
    volume_names: Optional[List[str]] = None
    implicit_complement: Optional[str] = None
    faceting_tolerance: Optional[float] = None
    volume_ids: Optional[np.ndarray] = None
    surface_ids: Optional[np.ndarray] = None
    #: free form information about how the mesh was made (backend, options, timings)
    info: dict = field(default_factory=dict)

    def __post_init__(self):
        self.vertices = np.ascontiguousarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.triangles = np.ascontiguousarray(self.triangles, dtype=np.int64).reshape(-1, 3)
        self.surface_offsets = np.ascontiguousarray(self.surface_offsets, dtype=np.int64)
        self.surface_senses = np.ascontiguousarray(self.surface_senses, dtype=np.int64).reshape(-1, 2)
        self.volume_tags = list(self.volume_tags)
        if self.volume_names is not None:
            self.volume_names = list(self.volume_names)
        if self.volume_ids is None:
            self.volume_ids = np.arange(1, self.n_volumes + 1, dtype=np.int64)
        else:
            self.volume_ids = np.asarray(self.volume_ids, dtype=np.int64)
        if self.surface_ids is None:
            self.surface_ids = np.arange(1, self.n_surfaces + 1, dtype=np.int64)
        else:
            self.surface_ids = np.asarray(self.surface_ids, dtype=np.int64)
        self._check_shapes()

    def _check_shapes(self):
        ns = self.n_surfaces
        if len(self.surface_offsets) == 0 or self.surface_offsets[0] != 0:
            raise ValueError("surface_offsets must start with 0")
        if self.surface_offsets[-1] != len(self.triangles):
            raise ValueError("surface_offsets must end with the number of triangles")
        if np.any(np.diff(self.surface_offsets) < 0):
            raise ValueError("surface_offsets must not decrease")
        if len(self.surface_senses) != ns or len(self.surface_ids) != ns:
            raise ValueError("surface_senses and surface_ids need one entry per surface")
        if len(self.volume_ids) != self.n_volumes:
            raise ValueError("volume_ids needs one entry per volume")
        if self.volume_names is not None and len(self.volume_names) != self.n_volumes:
            raise ValueError("volume_names needs one entry per volume")
        if len(self.triangles) and (self.triangles.min() < 0 or self.triangles.max() >= len(self.vertices)):
            raise ValueError("a triangle refers to a vertex that does not exist")
        if ns and (self.surface_senses.min() < NO_VOLUME or self.surface_senses.max() >= self.n_volumes):
            raise ValueError("a surface refers to a volume that does not exist")

    # ------------------------------------------------------------------ sizes
    @property
    def n_volumes(self) -> int:
        return len(self.volume_tags)

    @property
    def n_surfaces(self) -> int:
        return len(self.surface_offsets) - 1

    @property
    def n_triangles(self) -> int:
        return len(self.triangles)

    @property
    def n_vertices(self) -> int:
        return len(self.vertices)

    # --------------------------------------------------------------- queries
    def surface_triangles(self, surface: int) -> np.ndarray:
        """The triangles of a surface, oriented out of its forward volume."""
        return self.triangles[self.surface_offsets[surface]:self.surface_offsets[surface + 1]]

    def triangle_surfaces(self) -> np.ndarray:
        """The surface index of every triangle."""
        return np.repeat(np.arange(self.n_surfaces), np.diff(self.surface_offsets))

    def volume_surfaces(self, volume: int):
        """Surfaces bounding a volume.

        Returns
        -------
        forward, reverse : int arrays
            Indices of the surfaces whose normals point out of, respectively
            into, the volume.
        """
        forward = np.flatnonzero(self.surface_senses[:, 0] == volume)
        reverse = np.flatnonzero(self.surface_senses[:, 1] == volume)
        return forward, reverse

    def volume_triangles(self, volume: int) -> np.ndarray:
        """All triangles bounding a volume, oriented out of the volume."""
        forward, reverse = self.volume_surfaces(volume)
        parts = [self.surface_triangles(s) for s in forward]
        parts += [self.surface_triangles(s)[:, ::-1] for s in reverse]
        if not parts:
            return np.empty((0, 3), dtype=np.int64)
        return np.concatenate(parts)

    def bounding_box(self):
        """(lower corner, upper corner) of all vertices that are used by a triangle."""
        used = self.vertices[np.unique(self.triangles)] if self.n_triangles else self.vertices
        return used.min(axis=0), used.max(axis=0)

    # ------------------------------------------------------------- combining
    @classmethod
    def concatenate(cls, models: Sequence["MeshedModel"], implicit_complement: Optional[str] = None) -> "MeshedModel":
        """Join several models that do not share volumes into one.

        Volumes and surfaces are renumbered 1..n in the order of the models, so
        the ids in the result are unique.
        """
        models = list(models)
        if not models:
            raise ValueError("need at least one model")
        v_off = np.cumsum([0] + [m.n_vertices for m in models])
        vol_off = np.cumsum([0] + [m.n_volumes for m in models])
        tri_off = np.cumsum([0] + [m.n_triangles for m in models])

        senses = []
        for m, off in zip(models, vol_off):
            s = m.surface_senses.copy()
            s[s != NO_VOLUME] += off
            senses.append(s)
        offsets = [np.zeros(1, dtype=np.int64)]
        offsets += [m.surface_offsets[1:] + off for m, off in zip(models, tri_off)]
        names = None
        if any(m.volume_names is not None for m in models):
            names = []
            for m in models:
                names += m.volume_names if m.volume_names is not None else [""] * m.n_volumes
        tolerances = [m.faceting_tolerance for m in models if m.faceting_tolerance is not None]
        if implicit_complement is None:
            implicit_complement = next(
                (m.implicit_complement for m in reversed(models) if m.implicit_complement is not None), None
            )
        return cls(
            vertices=np.concatenate([m.vertices for m in models]),
            triangles=np.concatenate([m.triangles + off for m, off in zip(models, v_off)]),
            surface_offsets=np.concatenate(offsets),
            surface_senses=np.concatenate(senses),
            volume_tags=[t for m in models for t in m.volume_tags],
            volume_names=names,
            implicit_complement=implicit_complement,
            faceting_tolerance=max(tolerances) if tolerances else None,
        )

    def summary(self) -> str:
        shared = int(np.count_nonzero(self.surface_senses[:, 1] != NO_VOLUME)) if self.n_surfaces else 0
        return (
            f"{self.n_volumes} volumes, {self.n_surfaces} surfaces ({shared} shared), "
            f"{self.n_triangles} triangles, {self.n_vertices} vertices"
        )
