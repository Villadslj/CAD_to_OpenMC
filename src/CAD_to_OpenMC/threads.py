"""Control over the number of threads OpenCASCADE uses.

OpenCASCADE runs its parallel algorithms (boolean operations, triangulation)
on one process wide thread pool. Builds that include Intel TBB hand parallel
loops to TBB instead, which ignores the size of that pool. To make a requested
number of threads binding on every build, OpenCASCADE is told to use its own
pool for the duration of the work.
"""
from __future__ import annotations

from contextlib import contextmanager

from OCP.OSD import OSD_Parallel, OSD_ThreadPool


def occ_thread_count() -> int:
    """Number of threads of OpenCASCADE's pool, the calling thread included."""
    return OSD_ThreadPool.DefaultPool_s().NbThreads()


@contextmanager
def occ_threads(threads: int):
    """Let OpenCASCADE use exactly `threads` threads inside the with-block.

    The previous pool size and the choice between OpenCASCADE's own threads
    and TBB are restored afterwards, since both are process wide settings.
    """
    pool = OSD_ThreadPool.DefaultPool_s()
    previous_size = pool.NbThreads()
    previous_own = OSD_Parallel.ToUseOcctThreads_s()
    OSD_Parallel.SetUseOcctThreads_s(True)
    pool.Init(threads)
    try:
        yield
    finally:
        pool.Init(previous_size)
        OSD_Parallel.SetUseOcctThreads_s(previous_own)
