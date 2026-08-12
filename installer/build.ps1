# Build the single-file installer exe (mouse-mesh-pipeline-setup.exe).
#
#   pip install pyinstaller
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# Assembles a payload/ from the repo (pipeline scripts + configs + prebuilt
# Windows C++ binaries in bin/win64) and freezes mmp_setup.py around it. The
# installer itself is small; heavy libs (vtk/pymeshlab/...) are pip-installed on
# the target machine at install time.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$payload = Join-Path $here "payload"

Remove-Item -Recurse -Force $payload, (Join-Path $here "build"), (Join-Path $here "dist") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $payload | Out-Null

# --- assemble the payload ------------------------------------------------
Copy-Item (Join-Path $repo "*.py") $payload
Copy-Item (Join-Path $repo "README.md"), (Join-Path $repo "LICENSE") $payload
foreach ($d in "cgal_remesh", "configs", "scripts", "bin") {
    Copy-Item -Recurse (Join-Path $repo $d) $payload
}
# ship the FULL pip requirements (numpy/scipy/... included), not the conda-extras one
Copy-Item (Join-Path $here "requirements.txt") (Join-Path $payload "requirements.txt") -Force
# the C++ exes live in bin/win64 — drop any stray build outputs from cgal_remesh
Remove-Item (Join-Path $payload "cgal_remesh\*.exe"), (Join-Path $payload "cgal_remesh\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

# --- freeze --------------------------------------------------------------
Push-Location $here
try {
    # PyInstaller logs INFO to stderr; under -ErrorActionPreference Stop PS 5.1
    # would treat that as a terminating NativeCommand error. Use $LASTEXITCODE.
    $ErrorActionPreference = "Continue"
    pyinstaller --noconfirm --onefile --windowed --name mouse-mesh-pipeline-setup `
        --add-data "payload;payload" mmp_setup.py
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
    Write-Host "`nBuilt: $(Join-Path $here 'dist\mouse-mesh-pipeline-setup.exe')"
}
finally { Pop-Location }
