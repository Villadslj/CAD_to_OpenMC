"""Options of the in-memory OpenCASCADE mesher.

The options are derived from the ``mesher_config`` dictionary of the assembly
module, so existing scripts keep working. Each backend keeps the meaning it
always had for ``tolerance``:

* ``stl2``: Watson algorithm, tolerance relative to the size of each edge
* ``db``:   Delabella algorithm, tolerance as an absolute distance
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: (algorithm, tolerance mode) each backend has used so far
BACKEND_DEFAULTS = {
    "stl2": ("watson", "relative"),
    "db": ("delabella", "absolute"),
}


def available_cores() -> int:
    """Number of cores this process is allowed to use."""
    counter = getattr(os, "process_cpu_count", None)  # python 3.13 and later
    if counter is not None:
        return counter() or 1
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:  # not available on this platform
        return os.cpu_count() or 1


def resolve_threads(value) -> int:
    """Turn a thread setting into a positive number: None, 0 and 'all' mean every available core."""
    if value is None or value == "all" or (value == 0 and not isinstance(value, bool)):
        return available_cores()
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"threads must be None, 'all' or a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class MeshOptions:
    """How to triangulate.

    Parameters
    ----------
    tolerance : float
        Largest allowed distance between a triangle and the CAD surface. In
        relative mode this is a fraction of the size of each edge, in absolute
        mode it is a length in the units of the model (cm).
    angular_tolerance : float
        Largest allowed angle, in radians, between neighbouring segments along
        a curved edge.
    tolerance_mode : 'relative' or 'absolute'
    algorithm : 'watson' or 'delabella'
    threads : int, optional
        Number of threads OpenCASCADE may use. None uses all available cores.
        The mesh does not depend on the number of threads.
    grouping : 'model' or 'solid'
        'model' triangulates all solids in one go, which is what makes use of
        several threads. 'solid' triangulates one solid at a time, smallest
        first, and reproduces the mesh density of the STL-based pipeline.
    relative_factor : float
        Factor applied to a relative tolerance when several solids are
        triangulated together. OpenCASCADE loosens a relative tolerance by up
        to a factor of four for edges that are small compared to the whole
        shape; 0.25 makes sure that no edge is meshed coarser than it would be
        if its solid were triangulated on its own.
    strict : bool
        Refuse to write a geometry in which a face could not be triangulated or
        a volume is not closed.
    """

    tolerance: float = 0.1
    angular_tolerance: float = 0.2
    tolerance_mode: str = "relative"
    algorithm: str = "watson"
    threads: Optional[int] = None
    grouping: str = "model"
    relative_factor: float = 0.25
    strict: bool = True

    def __post_init__(self):
        if not self.tolerance > 0 or not self.angular_tolerance > 0:
            raise ValueError("tolerance and angular_tolerance must be positive")
        if self.tolerance_mode not in ("relative", "absolute"):
            raise ValueError(f"tolerance_mode must be 'relative' or 'absolute', got {self.tolerance_mode!r}")
        if self.algorithm not in ("watson", "delabella"):
            raise ValueError(f"algorithm must be 'watson' or 'delabella', got {self.algorithm!r}")
        if self.grouping not in ("model", "solid"):
            raise ValueError(f"mesh_grouping must be 'model' or 'solid', got {self.grouping!r}")
        if not 0 < self.relative_factor <= 1:
            raise ValueError("relative_tolerance_factor must be larger than 0 and at most 1")
        object.__setattr__(self, "threads", resolve_threads(self.threads))

    @classmethod
    def from_config(cls, config: dict, backend: str) -> "MeshOptions":
        """Build the options for a backend from a mesher_config dictionary."""
        try:
            algorithm, mode = BACKEND_DEFAULTS[backend]
        except KeyError:
            raise ValueError(f"backend '{backend}' is not meshed with OpenCASCADE") from None
        return cls(
            tolerance=config.get("tolerance", cls.tolerance),
            angular_tolerance=config.get("angular_tolerance", cls.angular_tolerance),
            tolerance_mode=config.get("tolerance_mode") or mode,
            algorithm=algorithm,
            threads=config.get("threads"),
            grouping=config.get("mesh_grouping") or cls.grouping,
            relative_factor=config.get("relative_tolerance_factor") or cls.relative_factor,
            strict=bool(config.get("strict", True)),
        )
