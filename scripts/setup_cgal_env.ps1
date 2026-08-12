<#
    Deploys the CGAL conda environment for conformal tetrahedral meshing.

    This environment runs only:
        npy2conformal_mesh.py --cgal   (pygalmesh / CGAL single tet mesh + surfaces)

    pygalmesh requires the CGAL, Eigen and MPFR native libraries. Those come
    for free from the conda-forge build of pygalmesh, so we install it (and its
    scientific companions) from conda-forge, then add three pip-only utilities.

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\setup_cgal_env.ps1
        powershell ... -File scripts\setup_cgal_env.ps1 -EnvName cgal_env

    After deployment, point the GUI field 'Python cgal_env' at:
        <conda-root>\envs\<EnvName>\python.exe
#>
param(
    [string]$EnvName = "cgal_env"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Creating conda env '$EnvName' (python 3.10, conda-forge)..." -ForegroundColor Cyan
conda create -n $EnvName python=3.10 -c conda-forge -y

Write-Host "==> Installing pygalmesh + native/scientific deps (conda-forge)..." -ForegroundColor Cyan
conda install -n $EnvName -c conda-forge `
    "pygalmesh=0.10.7" "meshio=5.3.5" "numpy=2.2" "scipy=1.15" `
    pyvista networkx rtree -y

Write-Host "==> Installing pip-only mesh utilities..." -ForegroundColor Cyan
conda run -n $EnvName --no-capture-output pip install -r "$Here\requirements-cgal.txt"

Write-Host ""
Write-Host "==> Verifying pygalmesh import..." -ForegroundColor Cyan
conda run -n $EnvName --no-capture-output python -c "import pygalmesh, meshio, pymeshfix, fast_simplification; print('pygalmesh', pygalmesh.__version__, 'OK')"

Write-Host ""
Write-Host "Done. Env python:" -ForegroundColor Green
conda run -n $EnvName --no-capture-output python -c "import sys; print(sys.executable)"
