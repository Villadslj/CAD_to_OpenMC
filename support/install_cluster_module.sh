#!/usr/bin/env bash
# Install this fork of CAD_to_OpenMC into a self-contained conda environment and
# write an environment-module file for it, e.g. for use on a Slurm cluster.
#
# 'conda install -c conda-forge cad-to-openmc' installs the upstream package, not
# this fork. This script therefore takes the dependencies (OCP, cadquery, gmsh,
# MOAB/pymoab, ...) from conda-forge and pip-installs CAD_to_OpenMC itself from
# this repository, without letting pip touch the dependencies.
#
# Requirements: conda, mamba or micromamba on the PATH (e.g. after
# 'module load conda'/'module load anaconda'), and internet access on the node
# running the script (normally a login node). git is needed only if the source is
# a git URL. The environment takes about 3 GB.
#
# Python 3.11 is the default. With 3.9 the conda-forge dependency solve did not
# finish, as current cadquery/gmsh builds need python >= 3.10.
#
# Usage:
#   support/install_cluster_module.sh [options]
#
# Then:
#   module use <modulefiles-dir>
#   module load cad_to_openmc
#   python -c 'import CAD_to_OpenMC.assembly'
#
# Run with --help for the options.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://github.com/Villadslj/CAD_to_OpenMC.git"

PREFIX="${PREFIX:-$HOME/software}"
MODULEFILES="${MODULEFILES:-$HOME/modulefiles}"
MODULE_NAME="cad_to_openmc"
MODULE_VERSION=""
PYTHON_VERSION="3.11"
SOURCE=""
REF="main"
WITH_OPENMC=0
OPENMC_SPEC="${OPENMC_SPEC:-openmc=*=dagmc_nompi*}"
# Without a fixed hdf5 the solver can run for hours: ocp<7.9 (vtk 9.3), moab<5.6
# and openmc each accept many hdf5 versions. 1.14.3 suits all of them.
HDF5_SPEC="${HDF5_SPEC:-hdf5=1.14.3}"
MODULE_FORMAT="tcl"
CONDA_EXE_ARG=""
FORCE=0
SHARED=0
RUN_TEST=1

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --prefix DIR          software root; the environment is created in
                        DIR/<name>/<version>                  [$PREFIX]
  --modulefiles DIR     modulefile root; the module is written to
                        DIR/<name>/<version>                  [$MODULEFILES]
  --name NAME           module name                           [$MODULE_NAME]
  --version VERSION     module version   [<package version>-<git commit>]
  --python VERSION      python version                        [$PYTHON_VERSION]
  --source PATH|URL     CAD_to_OpenMC source: a local checkout or a git URL
                        [the checkout containing this script, else $REPO_URL]
  --ref REF             branch, tag or commit when --source is a git URL [$REF]
  --with-openmc         also install OpenMC with DAGMC from conda-forge
                        (spec set by \$OPENMC_SPEC)           [$OPENMC_SPEC]
  --module-format FMT   'tcl' (Environment Modules and Lmod) or 'lua' (Lmod)
                                                              [$MODULE_FORMAT]
  --conda CMD           conda executable  [mamba, conda or micromamba on PATH]
  --shared              make the installation readable for all users
  --force               replace an existing installation of the same version
  --no-test             skip the conversion test after installation
  -h, --help            show this help

PREFIX and MODULEFILES may also be given as environment variables. HDF5_SPEC
[$HDF5_SPEC] pins hdf5 to keep the dependency solve fast; set it empty to unpin.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
need_arg() { [[ $# -ge 2 && -n "$2" ]] || die "option $1 needs an argument"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)        need_arg "$@"; PREFIX="$2"; shift 2 ;;
    --modulefiles)   need_arg "$@"; MODULEFILES="$2"; shift 2 ;;
    --name)          need_arg "$@"; MODULE_NAME="$2"; shift 2 ;;
    --version)       need_arg "$@"; MODULE_VERSION="$2"; shift 2 ;;
    --python)        need_arg "$@"; PYTHON_VERSION="$2"; shift 2 ;;
    --source)        need_arg "$@"; SOURCE="$2"; shift 2 ;;
    --ref)           need_arg "$@"; REF="$2"; shift 2 ;;
    --with-openmc)   WITH_OPENMC=1; shift ;;
    --module-format) need_arg "$@"; MODULE_FORMAT="$2"; shift 2 ;;
    --conda)         need_arg "$@"; CONDA_EXE_ARG="$2"; shift 2 ;;
    --shared)        SHARED=1; shift ;;
    --force)         FORCE=1; shift ;;
    --no-test)       RUN_TEST=0; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)               usage >&2; die "unknown option: $1" ;;
  esac
done

[[ "$MODULE_FORMAT" == tcl || "$MODULE_FORMAT" == lua ]] \
  || die "--module-format must be 'tcl' or 'lua'"

# --- conda --------------------------------------------------------------------
if [[ -n "$CONDA_EXE_ARG" ]]; then
  CONDA="$CONDA_EXE_ARG"
else
  CONDA=""
  for c in mamba conda micromamba; do
    if command -v "$c" >/dev/null 2>&1; then CONDA="$c"; break; fi
  done
  [[ -n "$CONDA" ]] || die "no conda, mamba or micromamba found on the PATH. Load your conda module first, or use --conda."
fi
command -v "$CONDA" >/dev/null 2>&1 || die "'$CONDA' not found"

# --- source -------------------------------------------------------------------
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/c2omc_install.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

if [[ -z "$SOURCE" ]]; then
  if [[ -f "$SCRIPT_DIR/../pyproject.toml" && -d "$SCRIPT_DIR/../src/CAD_to_OpenMC" ]]; then
    SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)"
  else
    SOURCE="$REPO_URL"
  fi
fi

if [[ -d "$SOURCE" ]]; then
  SRC_DIR="$(cd "$SOURCE" && pwd)"
  [[ -f "$SRC_DIR/pyproject.toml" ]] || die "$SRC_DIR does not look like a CAD_to_OpenMC checkout"
else
  command -v git >/dev/null 2>&1 || die "git is needed to fetch $SOURCE"
  echo ">>> Fetching $SOURCE ($REF)"
  SRC_DIR="$WORKDIR/CAD_to_OpenMC"
  git clone --quiet "$SOURCE" "$SRC_DIR"
  git -C "$SRC_DIR" checkout --quiet "$REF"
fi

PKG_VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SRC_DIR/pyproject.toml" | head -n1)"
GIT_COMMIT=""
if command -v git >/dev/null 2>&1 && git -C "$SRC_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_COMMIT="$(git -C "$SRC_DIR" rev-parse --short HEAD)"
fi
if [[ -z "$MODULE_VERSION" ]]; then
  MODULE_VERSION="${PKG_VERSION:-unknown}${GIT_COMMIT:+-$GIT_COMMIT}"
fi

ENV_DIR="$PREFIX/$MODULE_NAME/$MODULE_VERSION"
if [[ "$MODULE_FORMAT" == lua ]]; then
  MODULE_FILE="$MODULEFILES/$MODULE_NAME/$MODULE_VERSION.lua"
else
  MODULE_FILE="$MODULEFILES/$MODULE_NAME/$MODULE_VERSION"
fi

echo ">>> CAD_to_OpenMC $PKG_VERSION${GIT_COMMIT:+ (commit $GIT_COMMIT)} from $SOURCE"
echo ">>> environment: $ENV_DIR"
echo ">>> modulefile:  $MODULE_FILE"
echo ">>> using:       $CONDA, python $PYTHON_VERSION"

if [[ -e "$ENV_DIR" ]]; then
  [[ $FORCE -eq 1 ]] || die "$ENV_DIR exists. Use --force to replace it, or --version to choose another version."
  echo ">>> Removing existing $ENV_DIR"
  rm -rf "$ENV_DIR"
fi

[[ $SHARED -eq 1 ]] && umask 022

# --- dependencies from conda-forge ---------------------------------------------
PACKAGES=(
  "python=$PYTHON_VERSION"
  pip
  "setuptools>=61,<81"  # pymoab 5.5.1 imports pkg_resources
  "ocp>=7.7,<7.9"  # OCP 7.9 drops the _s suffix of static methods used by CAD_to_OpenMC
  "cadquery>=2.2,<2.8"  # 2.8 requires ocp 7.9
  python-gmsh
  "moab>=5.3,<5.6"  # 5.6 no longer ships the python bindings (pymoab)
  numpy
  pyparsing
  networkx
  unidecode
  h5py
  trimesh
)
[[ -n "$HDF5_SPEC" ]] && PACKAGES+=("$HDF5_SPEC")
[[ $WITH_OPENMC -eq 1 ]] && PACKAGES+=("$OPENMC_SPEC")

echo ">>> Creating the conda environment (this can take a while)"
mkdir -p "$(dirname "$ENV_DIR")"
"$CONDA" create --yes --prefix "$ENV_DIR" --override-channels -c conda-forge "${PACKAGES[@]}"

PY="$ENV_DIR/bin/python"
[[ -x "$PY" ]] || die "environment creation failed: $PY not found"

# --- CAD_to_OpenMC itself ------------------------------------------------------
echo ">>> Installing CAD_to_OpenMC from $SRC_DIR"
# --no-deps: the dependencies come from conda-forge (on PyPI pymoab does not exist
# and OCP is named cadquery-ocp). --no-build-isolation: build with the setuptools
# of the environment instead of downloading one.
# Copy the source, so that build artefacts are not written into the checkout.
BUILD_SRC="$WORKDIR/src"
mkdir -p "$BUILD_SRC"
cp -r "$SRC_DIR/pyproject.toml" "$SRC_DIR/README.md" "$SRC_DIR/LICENSE" "$SRC_DIR/src" "$BUILD_SRC/"
PYTHONNOUSERSITE=1 "$PY" -m pip install --no-deps --no-build-isolation --no-cache-dir "$BUILD_SRC"

# The command-line scripts are not installed by the package. Ship them in bin/.
SHARE_DIR="$ENV_DIR/share/cad_to_openmc"
mkdir -p "$SHARE_DIR"
cp -r "$SRC_DIR/scripts" "$SHARE_DIR/"
[[ -d "$SRC_DIR/examples" ]] && cp -r "$SRC_DIR/examples" "$SHARE_DIR/"
for s in "$SRC_DIR"/scripts/*.py; do
  dest="$ENV_DIR/bin/$(basename "$s" .py)"
  { echo "#!$PY"; sed '1{/^#!/d}' "$s"; } > "$dest"
  chmod +x "$dest"
done

cat > "$SHARE_DIR/install_info.txt" <<EOF
CAD_to_OpenMC $PKG_VERSION
source:  $SOURCE
ref:     ${GIT_COMMIT:-n/a}
python:  $PYTHON_VERSION
openmc:  $([[ $WITH_OPENMC -eq 1 ]] && echo "$OPENMC_SPEC" || echo "not installed")
date:    $(date -u +%Y-%m-%dT%H:%M:%SZ)
by:      $(id -un)@$(hostname)
EOF
"$CONDA" list --prefix "$ENV_DIR" --explicit > "$SHARE_DIR/conda-explicit.txt" 2>/dev/null || true

# --- check ---------------------------------------------------------------------
echo ">>> Checking the installation"
PYTHONNOUSERSITE=1 "$PY" - <<'EOF'
import importlib
import CAD_to_OpenMC
import CAD_to_OpenMC.assembly as ab
from importlib import metadata
print(f"CAD_to_OpenMC {metadata.version('CAD_to_OpenMC')} at {CAD_to_OpenMC.__path__[0]}")
missing = []
for mod in ("OCP", "cadquery", "gmsh", "pymoab", "h5py", "networkx", "trimesh"):
    try:
        importlib.import_module(mod)
    except Exception as e:
        missing.append(f"{mod}: {e}")
if ab.nogmsh:
    missing.append("gmsh could not be loaded by CAD_to_OpenMC")
if missing:
    raise SystemExit("ERROR: dependencies failed to import:\n  " + "\n  ".join(missing))
print("all dependencies import")
EOF

if [[ $RUN_TEST -eq 1 ]]; then
  echo ">>> Converting a test geometry (step -> h5m)"
  TESTDIR="$WORKDIR/test"
  mkdir -p "$TESTDIR"
  (
    cd "$TESTDIR"
    PYTHONNOUSERSITE=1 "$PY" - <<'EOF'
import cadquery as cq
import h5py
import CAD_to_OpenMC.assembly as ab

cq.exporters.export(cq.Workplane().box(1, 1, 1), "box.step")
for backend in ("stl2", "gmsh"):
    a = ab.Assembly(["box.step"], verbose=0)
    a.run(backend=backend, h5m_filename=f"box_{backend}.h5m", tags={".*": "mat1"})
    with h5py.File(f"box_{backend}.h5m", "r") as f:
        n = f["tstt/nodes/coordinates"].shape[0]
    assert n >= 8, f"{backend}: only {n} vertices written"
    print(f"backend {backend}: OK ({n} vertices)")
EOF
  )
fi

if [[ $WITH_OPENMC -eq 1 ]]; then
  "$ENV_DIR/bin/openmc" --version
fi

# --- modulefile ----------------------------------------------------------------
echo ">>> Writing $MODULE_FILE"
mkdir -p "$(dirname "$MODULE_FILE")"
WHATIS="CAD_to_OpenMC $PKG_VERSION (Villadslj fork${GIT_COMMIT:+, commit $GIT_COMMIT}): convert CAD (step) geometries to DAGMC h5m files for OpenMC"
if [[ "$MODULE_FORMAT" == lua ]]; then
  cat > "$MODULE_FILE" <<EOF
-- -*- lua -*-
-- Generated by CAD_to_OpenMC/support/install_cluster_module.sh
help([[
$WHATIS

Python $PYTHON_VERSION environment with CAD_to_OpenMC and its dependencies.
  python -c 'import CAD_to_OpenMC.assembly'
  c2omc_step2h5m --help
Scripts and examples: $SHARE_DIR
]])
whatis("$WHATIS")
local root = "$ENV_DIR"
prepend_path("PATH", pathJoin(root, "bin"))
setenv("CAD_TO_OPENMC_ROOT", root)
setenv("PYTHONNOUSERSITE", "1")
EOF
else
  cat > "$MODULE_FILE" <<EOF
#%Module1.0
## Generated by CAD_to_OpenMC/support/install_cluster_module.sh
proc ModulesHelp { } {
    puts stderr "$WHATIS"
    puts stderr ""
    puts stderr "Python $PYTHON_VERSION environment with CAD_to_OpenMC and its dependencies."
    puts stderr "  python -c 'import CAD_to_OpenMC.assembly'"
    puts stderr "  c2omc_step2h5m --help"
    puts stderr "Scripts and examples: $SHARE_DIR"
}
module-whatis "$WHATIS"
conflict $MODULE_NAME
set root "$ENV_DIR"
prepend-path PATH \$root/bin
setenv CAD_TO_OPENMC_ROOT \$root
setenv PYTHONNOUSERSITE 1
EOF
fi

if [[ $SHARED -eq 1 ]]; then
  chmod -R a+rX "$ENV_DIR" "$MODULE_FILE"
fi

cat <<EOF

Done. To use it:
  module use $MODULEFILES
  module load $MODULE_NAME/$MODULE_VERSION

In a Slurm job script, put the same two lines before running python. Add
'module use $MODULEFILES' to your ~/.bashrc to make the module always visible.
EOF
