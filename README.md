# mouse-mesh-pipeline

Turn a **labeled voxel tissue atlas** (`.npy`) into clean, watertight **per-tissue
surfaces** (and/or a conformal tetrahedral mesh) suitable for **surface-based
Monte-Carlo light transport**. Built for a mouse atlas (skin / skull / brain
structures) but generalises to any labeled volume.

The headline output is a set of **nested outer-envelope surfaces** — the correct
geometric representation for the target Monte-Carlo solver (see
[Why nested envelopes](#why-nested-envelopes)).

---

## Pipeline at a glance

```
INFO.txt + atlas.npy
      │  parse_info_txt.py          (spacing, label names, crop)
      ▼
 connectivity_searcher.py           Этап 1  keep largest connected component / tissue
 small_area_closer.py               Этап 2  drop sub-threshold specks
 label_smoother.py                  Этап 3  (opt) multi-label voxel smoothing
      ▼  02_merged.npy
      ├── mesh mode ─────────────────────────────────────────────
      │   classic     npy2vtk.py → meshValidator.py    (independent iso-surfaces)
      │   cgal        npy2conformal_mesh.py            (pygalmesh single tet mesh)
      │   cgal-remesh npy2inr.py → mesh_and_remesh.exe → mc_mesh_check.py
      │   envelopes   build_envelopes.py               ★ recommended for surface-MC
      ▼
 surface_cleaner.py                 watertight seal + Taubin + outward normals
      ▼
 …/surfaces/surface_NN_<tissue>.vtk  +  optical_properties.csv
```

`pipeline_manager.py` is a Tkinter GUI that wires every stage (pre-steps,
mesh mode, cleaning) with the right interpreters and DLL paths.

## Repository layout

| path | what |
|---|---|
| `*.py` (root) | pipeline scripts — kept flat because they import each other by name (`from parse_info_txt import …`) |
| `build_envelopes.py` | ★ nested-envelope builder (the recommended surface-MC path) |
| `mc_mesh_check.py` | MC-suitability checker + per-tissue surface / MMC export |
| `surface_cleaner.py` | per-tissue watertight seal + Taubin smoothing + outward normals |
| `pipeline_manager.py` | Tkinter GUI orchestrating all modes |
| `cgal_remesh/` | C++ CGAL tools (`mesh_and_remesh.cpp`, `tet_remesh.cpp`) + `npy2inr.py` + build |
| `scripts/` | conda env setup + `requirements-*.txt` |
| `configs/` | example `pipeline_config*.json` (⚠ paths are machine-specific — edit for your box) |
| `docs/` | audit report; Doxygen HTML lands in `docs/html/` |
| `.github/workflows/ci.yml` | CI: Python lint+compile, Doxygen, C++ build (GitHub / Gitea) |

## Install

Two conda environments (numpy/scipy MKL base + pip extras):

```powershell
# main env (pipeline + GUI): numpy, scipy, meshio, trimesh, pymeshlab, pymeshfix, vtk …
scripts\setup_main_env.ps1        # or setup_main_env.sh
# cgal env (only for pygalmesh / npy2inr .inr writer)
scripts\setup_cgal_env.ps1
```

C++ tools (needs MSYS2 UCRT64 g++ + gmp/mpfr + **CGAL 6.0.1 headers**, header-only):

```powershell
cd cgal_remesh
powershell -ExecutionPolicy Bypass -File build.ps1          # sequential
powershell -ExecutionPolicy Bypass -File build.ps1 -Tbb     # parallel Mesh_3 (needs Intel TBB)
```
See `cgal_remesh/build.ps1` header for the exact CGAL 6.0.1 download (the MSYS2
5.5.2 package is **not** usable — its tetrahedral remeshing is broken).

## Usage — nested envelopes (recommended)

```powershell
python build_envelopes.py --config configs\pipeline_config_mouse.json ^
       --facet-size 0.10 --facet-distance 0.05 --taubin 40 ^
       --nest-margin 2 --seal-open-radius 2 --jobs 1
```
Produces `…/surfaces_envelopes/surface_NN_<tissue>.vtk` + `optical_properties.csv`.
Nesting comes from the config key `envelope_parents` (e.g. mouse
`{"2":1,"4":2,"5":2,"6":2,"7":2}` — skin ⊃ skull ⊃ brain).

Key knobs:
- `--facet-size` — surface detail (≈ voxel size is the sweet spot; finer just adds staircase).
- `--seal-tunnels` / `--seal-open-radius` — close see-through "arch" tunnels (skin genus → 0).
- `--nest-margin` — voxels an outer tissue must extend beyond its children so smoothed surfaces never cross (skull can't poke through skin).
- `--crop-recess` — pull inner tissues back from a crop plane so the skin alone caps the cut face (auto = nest-margin).
- `--decimate` — quadric-decimate the final surfaces to this fraction of faces (`0.5` = 50 %; watertight/nesting preserved).
- `--jobs` — parallel per-tissue meshing (use `--jobs 1` with the TBB exe).

Or run everything from the GUI: `python pipeline_manager.py` → mode **«Оболочки»**.

## Why nested envelopes

The target Monte-Carlo (`photonMove.cpp` / `mcml_intersection.cpp`) calls
`FindIntersectionLayer(surfaceId, layerId)` with **no position and no normal**, so
the layer on the far side of a surface must be uniquely fixed by *(which surface,
which layer)*. That is only consistent under **strict nesting**, where each tissue
is represented by its **outer envelope** (air ⊃ skin ⊃ skull ⊃ brain);
`layerId == 0` is ambient/exit. A per-tissue *full* boundary (with inner walls
around nested organs) breaks this — the skin surface would face air on one part
and skull on another, and the skin|skull interface would be doubled.
`build_envelopes.py` builds exactly these nested shells via
`fill_holes(tissue ∪ nested-descendants)` → CGAL Mesh_3 → cleanup.

## Performance

Measured on the mouse envelopes (8-core box, facet 0.10). Full 6-tissue build:

| config | time | vs baseline |
|---|---|---|
| sequential exe, `--jobs 1` | 194 s | 1.0x |
| sequential exe, `--jobs 4` | 141 s | 1.4x |
| **TBB exe, `--jobs 1`** | **91 s** | **2.1x** |

- **GPU:** not applicable — CGAL Mesh_3 has no GPU backend; the voxel morphology (scipy) is already seconds.
- **Multi-thread across tissues:** `build_envelopes.py --jobs` / `surface_cleaner.py --jobs` run the independent tissues in parallel — bounded by the largest (skin), so ~1.4x.
- **Multi-thread within a mesh (the big lever):** `build.ps1 -Tbb` builds parallel CGAL Mesh_3 (`Parallel_tag`). The skin mesh alone drops **85 s -> 14 s (~6x)**. Needs Intel oneTBB built with the SAME gcc (the MSYS2 package conflicts with the pinned gcc 13; build oneTBB v2022.0.0 from source — see `cgal_remesh/build.ps1` header). With the TBB exe use **`--jobs 1`** (each mesh already uses all cores; `--jobs > 1` oversubscribes).

## Documentation (Doxygen)

```
doxygen Doxyfile        # -> docs/html/index.html   (C++ + Python docstrings)
```
CI builds this on every push and uploads it as the `doxygen-html` artifact.

## CI (GitHub / Gitea)

`.github/workflows/ci.yml` runs four jobs:
- **python** — ruff error-lint, `black --check` + `clang-format` format check, byte-compile every module.
- **docs** — build Doxygen HTML, upload as the `doxygen-html` artifact.
- **cpp** — build sequential + TBB against the pinned CGAL 6.0.1.
- **pages** — deploy the Doxygen HTML to GitHub Pages (default branch only).

Formatting is enforced by `black` (`pyproject.toml`, line length 100) and
`clang-format` (`.clang-format`). The syntax is standard GitHub Actions and runs
on **Gitea Actions** via `act_runner` (enable Actions and register a runner). The
`pages` deploy uses GitHub-only actions and is a no-op on Gitea — there, serve the
`doxygen-html` artifact or use Gitea Pages from a branch.

## License

MIT — see [LICENSE](LICENSE).
