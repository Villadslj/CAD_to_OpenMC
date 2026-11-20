"""Benchmark of the conversion of a step-file to a h5m-file.

Run from the root of the repository:

    python -m benchmarks.bench matrix --model lattice --size 6 --threads 1 2

Every configuration is run in a fresh process, so the timings and the peak
memory are not influenced by earlier runs. The conversion is split in the three
public steps of an Assembly: import, merge, and mesh + write. The h5m-file that
comes out is judged with CAD_to_OpenMC.validate, which is not part of the timing.

The synthetic models are generated on first use and cached in benchmarks/_models.
"""
import argparse
import json
import os
import pathlib
import platform
import resource
import subprocess
import sys
import tempfile
import time

MARKER = "BENCH_RESULT "
HERE = pathlib.Path(__file__).resolve().parent
MODEL_DIR = HERE / "_models"


def _generators():
    from tests import models

    return {
        "lattice": lambda n: models.pin_lattice(n),
        "pellets": lambda n: models.pellet_stack(n),
        "pellets-reversed": lambda n: models.pellet_stack(n, cladding_first=False),
        "shells": lambda n: models.nested_shells(n),
        "boxes": lambda n: models.separate_boxes(n),
    }


def model_file(name, size):
    """Path of the step-file of a synthetic model, generating it if needed."""
    from tests import models

    MODEL_DIR.mkdir(exist_ok=True)
    path = MODEL_DIR / f"{name}_{size}.step"
    if not path.exists():
        models.write_step(_generators()[name](size), path)
    return path


def run_single(args):
    """Convert one step-file in this process and print the measurements."""
    import CAD_to_OpenMC.assembly as ab
    from CAD_to_OpenMC.h5m_io import read_h5m
    from CAD_to_OpenMC.validate import check_model

    step = pathlib.Path(args.step).resolve()
    ab.mesher_config["tolerance"] = args.tolerance
    ab.mesher_config["angular_tolerance"] = args.angular_tolerance
    ab.mesher_config["threads"] = args.threads
    for item in args.config or []:
        key, value = item.split("=", 1)
        ab.mesher_config[key] = json.loads(value)

    phases = {}
    with tempfile.TemporaryDirectory() as workdir:
        os.chdir(workdir)
        out = pathlib.Path(workdir) / "out.h5m"
        start = time.perf_counter()
        assembly = ab.Assembly([str(step)], verbose=0)
        assembly.import_stp_files()
        phases["import"] = time.perf_counter() - start

        t = time.perf_counter()
        if args.merge:
            assembly.merge_all()
        phases["merge"] = time.perf_counter() - t

        t = time.perf_counter()
        assembly.solids_to_h5m(backend=args.backend, h5m_filename=str(out))
        phases["mesh_and_write"] = time.perf_counter() - t
        total = time.perf_counter() - start
        peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

        model = read_h5m(out)
        report = check_model(model)
        result = {
            "step": step.name,
            "backend": args.backend,
            "threads": args.threads,
            "merge": args.merge,
            "tolerance": args.tolerance,
            "angular_tolerance": args.angular_tolerance,
            "config": args.config or [],
            "seconds": {k: round(v, 4) for k, v in phases.items()},
            "total_seconds": round(total, 4),
            "peak_rss_mb": round(peak_rss_mb, 1),
            "h5m_bytes": out.stat().st_size,
            "volumes": model.n_volumes,
            "surfaces": model.n_surfaces,
            "triangles": model.n_triangles,
            "closed_volumes": sum(v.closed for v in report.volumes),
            "problems": len(report.problems()),
        }
        os.chdir(HERE.parent)
    print(MARKER + json.dumps(result))
    return 0


def _versions():
    from importlib import metadata

    out = {"python": platform.python_version(), "cpus": os.cpu_count(), "machine": platform.machine()}
    for package in ("CAD_to_OpenMC", "cadquery", "cadquery-ocp", "numpy", "gmsh", "h5py"):
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            out[package] = None
    return out


def run_matrix(args):
    """Run every combination of backend and thread count in its own process."""
    step = pathlib.Path(args.step).resolve() if args.step else model_file(args.model, args.size)
    results = []
    for backend in args.backends:
        for threads in args.threads:
            for repeat in range(args.repeat):
                cmd = [
                    sys.executable, "-m", "benchmarks.bench", "run", str(step),
                    "--backend", backend, "--threads", str(threads),
                    "--tolerance", str(args.tolerance), "--angular-tolerance", str(args.angular_tolerance),
                ]
                if not args.merge:
                    cmd.append("--no-merge")
                for item in args.config or []:
                    cmd += ["--config", item]
                done = subprocess.run(cmd, cwd=HERE.parent, capture_output=True, text=True)
                line = next((l for l in done.stdout.splitlines() if l.startswith(MARKER)), None)
                if done.returncode != 0 or line is None:
                    tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
                    result = {"step": step.name, "backend": backend, "threads": threads, "failed": " | ".join(tail)}
                else:
                    result = json.loads(line[len(MARKER):])
                result["repeat"] = repeat
                results.append(result)
                _print_row(result)
    if args.json:
        record = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "environment": _versions(), "results": results}
        with open(args.json, "w") as f:
            json.dump(record, f, indent=1)
    return 1 if any("failed" in r for r in results) else 0


def _print_row(r):
    if "failed" in r:
        print(f"{r['step']:22s} {r['backend']:12s} threads={r['threads']:<3d} FAILED: {r['failed']}")
        return
    s = r["seconds"]
    print(
        f"{r['step']:22s} {r['backend']:12s} threads={r['threads']:<3d} "
        f"import {s['import']:8.2f}s  merge {s['merge']:8.2f}s  mesh+write {s['mesh_and_write']:8.2f}s  "
        f"total {r['total_seconds']:8.2f}s  rss {r['peak_rss_mb']:7.0f} MB  "
        f"{r['volumes']} vol, {r['triangles']} tri, {r['closed_volumes']}/{r['volumes']} closed"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m benchmarks.bench", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--tolerance", type=float, default=0.001)
        p.add_argument("--angular-tolerance", type=float, default=0.2)
        p.add_argument("--no-merge", dest="merge", action="store_false")
        p.add_argument("--config", action="append", metavar="KEY=JSON", help="extra mesher_config entry")

    run = sub.add_parser("run", help="convert one step-file in this process")
    run.add_argument("step")
    run.add_argument("--backend", default="stl2")
    run.add_argument("--threads", type=int, default=1)
    common(run)

    matrix = sub.add_parser("matrix", help="run several configurations, each in a fresh process")
    matrix.add_argument("--step", help="a step-file of your own instead of a synthetic model")
    matrix.add_argument("--model", default="lattice", choices=["lattice", "pellets", "pellets-reversed", "shells", "boxes"])
    matrix.add_argument("--size", type=int, default=6)
    matrix.add_argument("--backends", nargs="+", default=["stl2"])
    matrix.add_argument("--threads", nargs="+", type=int, default=[1])
    matrix.add_argument("--repeat", type=int, default=1)
    matrix.add_argument("--json", metavar="FILE", help="write all measurements to FILE")
    common(matrix)

    args = parser.parse_args(argv)
    return run_single(args) if args.command == "run" else run_matrix(args)


if __name__ == "__main__":
    sys.exit(main())
