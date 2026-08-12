# cgal_remesh — CGAL C++ multi-material tetrahedral (re)meshing

Purpose: kill the surface "pimples"/spikes on the conformal tissue meshes
**without** breaking inter-tissue conformality, and keep the result a valid
Monte-Carlo (MMC) tetrahedral mesh.

## Why C++ / why these two tools

The tissues are nested and share exact interface vertices
(skin ⊃ skull ⊃ … ⊃ cerebrum). You cannot smooth one tissue's surface
independently — moving its copy of a shared wall reopens gaps at the interface.
The only conformal way to smooth shared interfaces is to remesh the whole
multi-material complex at once, which is what CGAL's **Tetrahedral Remeshing**
does. It is not exposed by `pygalmesh`, hence C++.

Two programs, because there are two situations:

| Tool | Input | What it does | When |
|------|-------|--------------|------|
| `tet_remesh` | existing Medit `.mesh` | load → `tetrahedral_isotropic_remeshing` → `.mesh` | only if the input has a **manifold boundary** |
| `mesh_and_remesh` | labeled image `.inr` | Mesh_3 → remesh → `.mesh` | the robust path (see below) |

### `tet_remesh` rarely applies — and here's the measured reason
CGAL's `Triangulation_3` cannot represent a tet mesh whose **outer boundary is
non-manifold**. The pipeline's `pygalmesh` output is exactly that — measured
**186 non-manifold boundary edges** (surface pinch points) on the mouse mesh —
so `read_MEDIT` fails while gluing the outer "infinite" cells
(`assign_neighbors`: a facet with ≠2 incident cells). You cannot remesh a mesh
you cannot load. `tet_remesh` is kept for meshes that *are* manifold (e.g. the
output of `mesh_and_remesh`, which can be re-remeshed at a different resolution).

### `mesh_and_remesh` is the working path
Meshes **and** remeshes inside CGAL, straight from the labeled voxel image, so
the data never leaves CGAL's valid structures:

1. read the labeled `.inr` (`CGAL::Image_3`);
2. `Labeled_mesh_domain_3` — **subdomain ids = the voxel label values**, so the
   Medit output already carries the true tissue labels (1,2,4,5,6,7…), no CGAL
   sequential-renumbering to undo downstream;
3. `make_mesh_3` with surface criteria (facet_size / facet_distance / facet_angle)
   + the default perturb/exude optimizers → well-shaped multi-material tets;
4. `tetrahedral_isotropic_remeshing` — isotropic elements + tangential boundary
   smoothing that removes the oversized "spike" triangles, preserving every
   subdomain interface;
5. `output_to_medit` → drop-in `.mesh`.

## Measured effect (mouse brain cores 4/5/6/7, target edge 0.2 mm)

| metric (per region, surface) | mesh only | mesh + remesh |
|------|-----------|---------------|
| median boundary edge | 0.33–0.34 mm | **0.18 mm** |
| **max** boundary edge (the spikes) | 0.57–0.59 mm | **0.28–0.41 mm** |
| p99/median (spike/outlier ratio) | ~1.56 | **~1.43** |
| tet quality q_median | ~0.82 | **0.886** |
| non-manifold surface edges (cerebrum) | 0 | **8** (was 601 on CGAL 5.5.2) |

Output validated **independently** of CGAL by `../mc_mesh_check.py`
(pure-numpy geometry): conformal (no face shared by >2 tets), per-material
sealed, **0 inverted tets, 0 slivers**, MC-READY.

## Build

Toolchain: MSYS2 UCRT64 `g++` + **CGAL 6.0.1 headers**. No MSVC required.
CGAL is header-only, so the 6.0.1 headers compile fine with the MSYS2 g++ and
its gmp/mpfr/boost/eigen — you do **not** use the (older) MSYS2 CGAL package.

```powershell
# 1) toolchain + math/boost/eigen libs (the cgal package is only pulled for these):
D:\msys64\usr\bin\pacman.exe -S --needed mingw-w64-ucrt-x86_64-gcc `
    mingw-w64-ucrt-x86_64-cgal mingw-w64-ucrt-x86_64-eigen3
# 2) CGAL 6.0.1 headers (header-only release) into ..\..\..\CGAL-6.0.1 :
curl -LO https://github.com/CGAL/cgal/releases/download/v6.0.1/CGAL-6.0.1-library.tar.xz
tar xf CGAL-6.0.1-library.tar.xz          # -> CGAL-6.0.1\
# 3) build:
powershell -ExecutionPolicy Bypass -File build.ps1
```

or directly (assertions ON — no -DNDEBUG):

```bash
CGAL=/g/nauchka/utilities/CGAL-6.0.1/include
g++ -O2 -std=c++17 mesh_and_remesh.cpp -o mesh_and_remesh.exe \
    -I"$CGAL" -I/d/msys64/ucrt64/include -L/d/msys64/ucrt64/lib -lmpfr -lgmp
```

### Why CGAL 6.0.1 specifically (not the MSYS2 5.5.2 package)
CGAL **5.5.2**'s tetrahedral remeshing crashes on over-strict `CGAL_assertion()`s
for a valid multi-material labeled-image mesh (`!is_on_convex_hull`,
`is_internal(e,…)`). Disabling assertions (`-DNDEBUG`) let it *complete*, but it
then silently produced **non-manifold junk** — 601 non-manifold edges on the
mouse skin, i.e. visible craters/spikes. **CGAL 6.0.1 fixed these bugs**: it runs
with assertions **on** and yields a clean mesh (0 inverted tets, 0 slivers, ~8
non-manifold edges). So the tool builds against 6.0.1 with assertions on.

The `manifold()` Mesh_3 criterion is still **off by default** (it looped/OOM'd in
5.5.2); `--manifold` opts in and is worth trying on 6.0.1 if a strictly manifold
surface is required.

## Usage in the pipeline

```bash
# 1) labeled volume -> INR  (cgal_env: uses pygalmesh.save_inr)
conda run -n cgal_env python cgal_remesh/npy2inr.py --config pipeline_config_mouse.json \
    --out <output_dir>/vtk_export/conformal/volume.inr

# 2) mesh + remesh  (run with MSYS2 ucrt64 bin on PATH for the DLLs)
cgal_remesh/mesh_and_remesh.exe volume.inr brain_full_conformal.mesh \
    --facet-size 0.4 --facet-distance 0.15 --cell-size 0.6 \
    --target-edge-length 0.4 --iterations 3

# 3) validate + export for MMC
conda run -n cgal_env python mc_mesh_check.py --mesh brain_full_conformal.mesh --export-mmc
```

This replaces the `npy2conformal_mesh.py --cgal` Tier-B step when smoother
surfaces are wanted; the produced `.mesh` uses the same Medit layout the rest of
the pipeline already reads.

## Files
- `mesh_and_remesh.cpp` — the working mesher+remesher (labeled image → `.mesh`).
- `tet_remesh.cpp` — remesh an existing manifold `.mesh` (see caveat above).
- `npy2inr.py` — `02_merged.npy` → `.inr` bridge (runs in cgal_env).
- `CMakeLists.txt`, `build.ps1` — build.
