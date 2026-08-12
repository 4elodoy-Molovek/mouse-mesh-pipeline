<#
    Deploys the MAIN conda environment for the medical-mesh pipeline.

    This environment runs:
      * the GUI              (pipeline_manager.py)
      * preprocessing        (connectivity_searcher.py, small_area_closer.py)
      * converters           (img2npy.py, rawb2npy.py)
      * classic VTK meshing  (npy2vtk.py, meshValidator.py)

    The conformal CGAL step (npy2conformal_mesh.py --cgal) uses a SEPARATE
    environment; deploy it with setup_cgal_env.ps1.

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\setup_main_env.ps1
        powershell ... -File scripts\setup_main_env.ps1 -EnvName myenv
#>
param(
    [string]$EnvName = "medmesh"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Creating conda env '$EnvName' (python 3.12)..." -ForegroundColor Cyan
conda create -n $EnvName python=3.12 -y

Write-Host "==> Installing MKL scientific base (conda)..." -ForegroundColor Cyan
conda install -n $EnvName numpy scipy pandas tqdm -y

Write-Host "==> Installing mesh & visualization libraries (pip)..." -ForegroundColor Cyan
conda run -n $EnvName --no-capture-output pip install -r "$Here\requirements-main.txt"

Write-Host ""
Write-Host "Done. Launch the pipeline GUI with:" -ForegroundColor Green
Write-Host "    conda run -n $EnvName python pipeline_manager.py"
Write-Host ""
Write-Host "In the GUI, set 'Python cgal_env' to the cgal environment's python.exe"
Write-Host "(created by setup_cgal_env.ps1) for the conformal CGAL meshing step."
