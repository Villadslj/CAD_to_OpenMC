#!/usr/bin/env bash
# Build HDF5, MOAB (with pymoab), DAGMC and OpenMC from source into a Python
# virtual environment.
#
# pymoab, DAGMC and OpenMC are not available from PyPI. The recommended way to
# get them is conda-forge. This script is the alternative for machines where
# conda cannot be used: everything is installed below the prefix of the virtual
# environment, nothing is written outside of it.
#
# Requirements: a C/C++ compiler, make, git, curl, and in the environment:
#   pip, cmake (3.18 to 3.31: MOAB 5.5.1 does not configure with CMake 4),
#   cython, numpy, setuptools<81 (pymoab 5.5.1 imports pkg_resources)
#
# Usage:
#   source .venv/bin/activate
#   support/build_dagmc_stack.sh [hdf5] [eigen] [moab] [dagmc] [openmc] [openmc-python]
#
# Without arguments all stages are run. A stage that has completed is skipped
# on the next run. Set JOBS to limit the number of parallel compile jobs.
set -euo pipefail

PREFIX="${PREFIX:-${VIRTUAL_ENV:?activate a virtual environment or set PREFIX}}"
WORK="${WORK:-$PREFIX/_build}"
JOBS="${JOBS:-$(nproc)}"
PYTHON="${PYTHON:-$PREFIX/bin/python}"

HDF5_VERSION="${HDF5_VERSION:-1.14.6}"
EIGEN_VERSION="${EIGEN_VERSION:-3.3.7}"
MOAB_VERSION="${MOAB_VERSION:-5.5.1}"
DAGMC_VERSION="${DAGMC_VERSION:-3.2.4}"
OPENMC_VERSION="${OPENMC_VERSION:-0.16.0}"

HDF5_URL="${HDF5_URL:-https://github.com/HDFGroup/hdf5/archive/refs/tags/hdf5_${HDF5_VERSION}.tar.gz}"
EIGEN_URL="${EIGEN_URL:-https://gitlab.com/libeigen/eigen/-/archive/${EIGEN_VERSION}/eigen-${EIGEN_VERSION}.tar.gz}"
MOAB_URL="${MOAB_URL:-https://ftp.mcs.anl.gov/pub/fathom/moab-${MOAB_VERSION}.tar.gz}"
DAGMC_URL="${DAGMC_URL:-https://github.com/svalinn/DAGMC/archive/refs/tags/v${DAGMC_VERSION}.tar.gz}"
OPENMC_GIT="${OPENMC_GIT:-https://github.com/openmc-dev/openmc.git}"

export PATH="$PREFIX/bin:$PATH"
export CMAKE_PREFIX_PATH="$PREFIX"
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# the python extension modules and the executables must find the libraries in the prefix
export LDFLAGS="-Wl,-rpath,$PREFIX/lib ${LDFLAGS:-}"

COMMON=(
  -DCMAKE_INSTALL_PREFIX="$PREFIX"
  -DCMAKE_INSTALL_LIBDIR=lib
  -DCMAKE_INSTALL_RPATH="$PREFIX/lib"
  -DCMAKE_BUILD_TYPE=Release
)

mkdir -p "$WORK"

fetch_tar() { # url destination
  rm -rf "$2"
  mkdir -p "$2"
  curl -fsSL "$1" | tar xz --strip-components=1 -C "$2"
}

build_install() { # source-dir build-dir [cmake options...]
  local src="$1" bld="$2"
  shift 2
  cmake -S "$src" -B "$bld" "${COMMON[@]}" "$@"
  cmake --build "$bld" -j "$JOBS"
  cmake --install "$bld"
}

stage_hdf5() {
  fetch_tar "$HDF5_URL" "$WORK/hdf5"
  build_install "$WORK/hdf5" "$WORK/hdf5-build" \
    -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF -DBUILD_TESTING=OFF \
    -DHDF5_BUILD_TOOLS=OFF -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_UTILS=OFF \
    -DHDF5_BUILD_HL_LIB=ON -DHDF5_BUILD_CPP_LIB=OFF -DHDF5_BUILD_FORTRAN=OFF \
    -DHDF5_ENABLE_Z_LIB_SUPPORT=OFF -DHDF5_ENABLE_SZIP_SUPPORT=OFF
}

stage_eigen() {
  # header only, used by MOAB instead of BLAS/LAPACK
  fetch_tar "$EIGEN_URL" "$WORK/eigen"
}

stage_moab() {
  fetch_tar "$MOAB_URL" "$WORK/moab"
  build_install "$WORK/moab" "$WORK/moab-build" \
    -DBUILD_SHARED_LIBS=ON -DENABLE_HDF5=ON -DHDF5_ROOT="$PREFIX" \
    -DENABLE_BLASLAPACK=OFF -DEIGEN3_DIR="$WORK/eigen" \
    -DENABLE_FORTRAN=OFF -DENABLE_TESTING=OFF \
    -DENABLE_PYMOAB=ON -DPYTHON_EXECUTABLE="$PYTHON"
  "$PYTHON" -c "from pymoab import core; core.Core(); print('pymoab ok')"
}

stage_dagmc() {
  fetch_tar "$DAGMC_URL" "$WORK/dagmc"
  build_install "$WORK/dagmc" "$WORK/dagmc-build" \
    -DMOAB_DIR="$PREFIX" -DBUILD_UWUW=OFF -DBUILD_TALLY=OFF -DBUILD_TESTS=OFF \
    -DBUILD_STATIC_LIBS=OFF -DBUILD_STATIC_EXE=OFF
}

stage_openmc() {
  rm -rf "$WORK/openmc"
  git clone --quiet --depth 1 -b "v$OPENMC_VERSION" --recurse-submodules --shallow-submodules \
    "$OPENMC_GIT" "$WORK/openmc"
  build_install "$WORK/openmc" "$WORK/openmc-build" \
    -DOPENMC_USE_DAGMC=ON -DOPENMC_USE_MPI=OFF -DOPENMC_BUILD_TESTS=OFF \
    -DHDF5_ROOT="$PREFIX" -DHDF5_PREFER_PARALLEL=OFF
  openmc --version
}

stage_openmc_python() {
  "$PYTHON" -m pip install "$WORK/openmc"
  "$PYTHON" -c "import openmc; print('openmc python api', openmc.__version__)"
}

if [ "$#" -eq 0 ]; then
  set -- hdf5 eigen moab dagmc openmc openmc-python
fi

for stage in "$@"; do
  stamp="$WORK/.done-$stage"
  if [ -e "$stamp" ]; then
    echo "=== $stage: already done, skipping (remove $stamp to rebuild)"
    continue
  fi
  echo "=== $stage: start $(date -u +%H:%M:%S)"
  "stage_${stage//-/_}"
  touch "$stamp"
  echo "=== $stage: done  $(date -u +%H:%M:%S)"
done
