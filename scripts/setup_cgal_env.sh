#!/usr/bin/env bash
#
# Deploys the CGAL conda environment for conformal tetrahedral meshing.
#
# This environment runs only:
#   npy2conformal_mesh.py --cgal   (pygalmesh / CGAL single tet mesh + surfaces)
#
# pygalmesh requires the CGAL, Eigen and MPFR native libraries. Those come for
# free from the conda-forge build of pygalmesh, so we install it (and its
# scientific companions) from conda-forge, then add three pip-only utilities.
#
# Usage:
#   bash scripts/setup_cgal_env.sh            # env name: cgal_env
#   bash scripts/setup_cgal_env.sh myenv      # custom env name
#
# After deployment, point the GUI field 'Python cgal_env' at:
#   <conda-root>/envs/<ENV_NAME>/python(.exe)
#
set -euo pipefail

ENV_NAME="${1:-cgal_env}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Creating conda env '${ENV_NAME}' (python 3.10, conda-forge)..."
conda create -n "${ENV_NAME}" python=3.10 -c conda-forge -y

echo "==> Installing pygalmesh + native/scientific deps (conda-forge)..."
conda install -n "${ENV_NAME}" -c conda-forge \
    "pygalmesh=0.10.7" "meshio=5.3.5" "numpy=2.2" "scipy=1.15" \
    pyvista networkx rtree -y

echo "==> Installing pip-only mesh utilities..."
conda run -n "${ENV_NAME}" --no-capture-output pip install -r "${HERE}/requirements-cgal.txt"

echo ""
echo "==> Verifying pygalmesh import..."
conda run -n "${ENV_NAME}" --no-capture-output python -c \
    "import pygalmesh, meshio, pymeshfix, fast_simplification; print('pygalmesh', pygalmesh.__version__, 'OK')"

echo ""
echo "Done. Env python:"
conda run -n "${ENV_NAME}" --no-capture-output python -c "import sys; print(sys.executable)"
