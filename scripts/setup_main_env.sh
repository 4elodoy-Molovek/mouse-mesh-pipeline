#!/usr/bin/env bash
#
# Deploys the MAIN conda environment for the medical-mesh pipeline.
#
# This environment runs:
#   * the GUI              (pipeline_manager.py)
#   * preprocessing        (connectivity_searcher.py, small_area_closer.py)
#   * converters           (img2npy.py, rawb2npy.py)
#   * classic VTK meshing  (npy2vtk.py, meshValidator.py)
#
# The conformal CGAL step (npy2conformal_mesh.py --cgal) uses a SEPARATE
# environment; deploy it with setup_cgal_env.sh.
#
# Usage:
#   bash scripts/setup_main_env.sh            # env name: medmesh
#   bash scripts/setup_main_env.sh myenv      # custom env name
#
set -euo pipefail

ENV_NAME="${1:-medmesh}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Creating conda env '${ENV_NAME}' (python 3.12)..."
conda create -n "${ENV_NAME}" python=3.12 -y

echo "==> Installing MKL scientific base (conda)..."
conda install -n "${ENV_NAME}" numpy scipy pandas tqdm -y

echo "==> Installing mesh & visualization libraries (pip)..."
conda run -n "${ENV_NAME}" --no-capture-output pip install -r "${HERE}/requirements-main.txt"

echo ""
echo "Done. Launch the pipeline GUI with:"
echo "    conda run -n ${ENV_NAME} python pipeline_manager.py"
echo ""
echo "In the GUI, set 'Python cgal_env' to the cgal environment's python"
echo "(created by setup_cgal_env.sh) for the conformal CGAL meshing step."
