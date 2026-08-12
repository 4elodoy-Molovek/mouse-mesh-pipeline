# Environment deployment scripts

The pipeline uses **two** conda environments:

| Environment | Python | Purpose | Deploy script |
|-------------|--------|---------|---------------|
| `medmesh` (main) | 3.12 | GUI, preprocessing, converters, classic VTK meshing | `setup_main_env.{ps1,sh}` |
| `cgal_env` | 3.10 | Conformal tetrahedral meshing (`pygalmesh`/CGAL) | `setup_cgal_env.{ps1,sh}` |

Each environment has both a PowerShell (`.ps1`) and a bash (`.sh`) deploy
script; use whichever matches your shell.

Two environments are needed because `pygalmesh` pins an older Python/NumPy ABI
and drags in the heavy CGAL/Eigen/MPFR native stack, which we keep isolated from
the main tooling.

## Prerequisites

- [Miniconda / Anaconda](https://docs.conda.io/) with `conda` available on `PATH`.

## Deploy

From the repository root (`project/utilities`):

PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_main_env.ps1
powershell -ExecutionPolicy Bypass -File scripts\setup_cgal_env.ps1
```

bash (Git Bash / WSL / Linux / macOS):

```bash
bash scripts/setup_main_env.sh
bash scripts/setup_cgal_env.sh
```

Both variants accept a custom environment name — `-EnvName <name>` for
PowerShell, or a positional argument for bash:

```powershell
powershell ... -File scripts\setup_cgal_env.ps1 -EnvName cgal_env_test
```
```bash
bash scripts/setup_cgal_env.sh cgal_env_test
```

## Run

Launch the GUI from the **main** environment:

```powershell
conda run -n medmesh python pipeline_manager.py
```

In the GUI:

1. **Section 0 — Config file**: pick `pipeline_config.json` (brain) or
   `pipeline_config_mouse.json` (mouse) and press **Загрузить** to populate all
   fields. Running writes the fields back to that same file and passes it to
   every step via `--config`.
2. **Python cgal_env**: point at the cgal environment's `python.exe`
   (`<conda-root>\envs\cgal_env\python.exe`) so the conformal CGAL step runs in
   the right interpreter.

## Running steps manually

Every pipeline script takes `--config`:

```powershell
# preprocessing
conda run -n medmesh  python connectivity_searcher.py --config pipeline_config_mouse.json
conda run -n medmesh  python small_area_closer.py     --config pipeline_config_mouse.json
conda run -n medmesh  python label_smoother.py        --config pipeline_config_mouse.json   # optional: voxel smoothing (σ = label_smooth_sigma)

# mesh mode A — pygalmesh (single env step)
conda run -n cgal_env python npy2conformal_mesh.py    --config pipeline_config_mouse.json --cgal

# mesh mode B — CGAL-remesh (C++, smoother surfaces). See ../cgal_remesh/README.md to build the .exe.
conda run -n cgal_env python cgal_remesh\npy2inr.py   --config pipeline_config_mouse.json --out <out>\vtk_export\conformal\volume.inr
# (run mesh_and_remesh.exe with D:\msys64\ucrt64\bin on PATH)
cgal_remesh\mesh_and_remesh.exe volume.inr brain_full_conformal.mesh --facet-size 0.4 --target-edge-length 0.4 --iterations 2
conda run -n medmesh  python mc_mesh_check.py         --config pipeline_config_mouse.json --export-surfaces --export-mmc

# mesh mode C — Envelopes (nested outer shells; the CORRECT input for the surface-MC).
# Each tissue -> fill_holes(tissue u nested) -> CGAL -> outer shell -> surface_cleaner.
# Nesting comes from config key "envelope_parents" (e.g. {"2":1,"4":2,"5":2,"6":2,"7":2}).
conda run -n medmesh  python build_envelopes.py       --config pipeline_config_mouse.json --facet-size 0.10 --facet-distance 0.05 --taubin 30
```

The GUI (`pipeline_manager.py`) wires all of this: the **Этап 3** checkbox runs
`label_smoother.py`; the **CGAL-remesh (C++)** mesh mode chains
`npy2inr → mesh_and_remesh.exe → mc_mesh_check`; the **Оболочки (build_envelopes)**
mode builds the nested per-tissue envelopes — all with the right interpreters and
the MSYS2 DLL `PATH`. Why envelopes: the surface-MC's `FindIntersectionLayer(surfaceId,
layerId)` takes no position, so each surface must separate exactly two layers ⇒
strict nesting ⇒ each tissue = its outer envelope (not its full boundary).

## Files

- `requirements-main.txt` — pip libraries for the main env (installed after the
  conda scientific base).
- `requirements-cgal.txt` — pip-only libraries for the cgal env (`pygalmesh`,
  `meshio`, `scipy`, `numpy`, `pyvista` come from conda-forge instead).
