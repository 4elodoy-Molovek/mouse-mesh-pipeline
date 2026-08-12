# -*- coding: utf-8 -*-
"""
Per-tissue surface cleanup for the extracted CGAL surfaces (pymeshlab / vcglib).

The surfaces extracted from the tet mesh inherit its boundary defects: voxel-
scale spikes, non-manifold pinches (render as holes) and small flying fragments.
This polishes each surface to a clean, smooth, watertight mesh:

    1. remove duplicate/unreferenced vertices and duplicate faces
    2. repair non-manifold edges/vertices  (fixes the "holes")
    3. drop small disconnected components   (removes flying fragments)
    4. close remaining holes
    5. Taubin de-spike                        (removes the spikes; volume-preserving)
    6. optional isotropic remeshing           (uniform triangles; --remesh)
    7. light Taubin polish

Taubin de-spiking keeps the polygon count (good when you want a dense mesh);
--remesh re-tiles to a target edge length (may lower the count).

NOTE ON CONFORMALITY: this cleans each tissue independently, so shared tissue
interfaces may drift slightly — great for visualisation / surface (MCX) MC, but
the *conformal* artefact for volumetric MMC stays the tet mesh
(brain_full_conformal.mesh / .node/.elem), which is not modified here.

Usage:
    python surface_cleaner.py --dir <folder with surface_*.vtk>
    python surface_cleaner.py --config pipeline_config_mouse.json   # cleans vtk_export/conformal
    python surface_cleaner.py --dir HQ --remesh --target 0.2 --min-faces 1000 --taubin 25
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _tri(path):
    import meshio

    m = meshio.read(path)
    tri = next((cb.data for cb in m.cells if cb.type == "triangle"), None)
    return (np.asarray(m.points), np.asarray(tri)) if tri is not None else (None, None)


def _metrics(P, F):
    import scipy.sparse as sp
    import scipy.sparse.csgraph as csg

    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [0, 2]]]), axis=1)
    ue, ec = np.unique(e, axis=0, return_counts=True)
    el = np.linalg.norm(P[ue[:, 0]] - P[ue[:, 1]], axis=1).mean()
    n = len(P)
    A = sp.csr_matrix(
        (
            np.ones(len(ue) * 2),
            (np.concatenate([ue[:, 0], ue[:, 1]]), np.concatenate([ue[:, 1], ue[:, 0]])),
        ),
        shape=(n, n),
    )
    deg = np.asarray(A.sum(1)).ravel()
    deg[deg == 0] = 1
    disp = np.linalg.norm(P - (sp.diags(1 / deg) @ A @ P), axis=1)[np.unique(F)]
    ncomp = len(np.unique(csg.connected_components(A, directed=False)[1][np.unique(F)]))
    return (
        f"faces={len(F):>7} comp={ncomp} nonmanif={int(np.sum(ec > 2)):>4} "
        f"holes={int(np.sum(ec == 1)):>4} spike_p99={np.percentile(disp, 99):.3f}mm "
        f"spike_max={disp.max():.3f}mm"
    )


def _connected_faces(P, F):
    """Return the connected-component id of each face (via the vertex graph)."""
    import scipy.sparse as sp
    import scipy.sparse.csgraph as csg

    e = np.unique(
        np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [0, 2]]]), axis=1), axis=0
    )
    n = len(P)
    A = sp.csr_matrix(
        (
            np.ones(len(e) * 2),
            (np.concatenate([e[:, 0], e[:, 1]]), np.concatenate([e[:, 1], e[:, 0]])),
        ),
        shape=(n, n),
    )
    return csg.connected_components(A, directed=False)[1][F[:, 0]]


def _seal_per_component(P, F):
    """Make every connected component watertight+manifold with pymeshfix,
    SEPARATELY (so multi-part tissues like the 3 skull bones are all kept —
    pymeshfix on a whole multi-component mesh keeps only one part)."""
    import pymeshfix

    fc = _connected_faces(P, F)
    outP, outF, off = [], [], 0
    for cid in np.unique(fc):
        Fc = F[fc == cid]
        used = np.unique(Fc)
        remap = np.zeros(len(P), dtype=np.int64)
        remap[used] = np.arange(len(used))
        mf = pymeshfix.MeshFix(P[used].astype(np.float64), remap[Fc].astype(np.int32))
        mf.repair(joincomp=False, remove_smallest_components=False)
        Ps, Fs = np.asarray(mf.points), np.asarray(mf.faces)
        if len(Fs) == 0:
            continue
        outP.append(Ps)
        outF.append(Fs + off)
        off += len(Ps)
    return np.vstack(outP), np.vstack(outF)


def _clean_file(job):
    """Worker for parallel cleaning (module-level so it pickles on Windows spawn).
    job = (src_path, out_dir, target, taubin, min_faces, remesh, seal, decimate)."""
    import meshio

    s, out, target, taubin, min_faces, remesh, seal, decimate = job
    name = os.path.basename(s)
    P, F = _tri(s)
    if P is None:
        return (name, None, None)
    before = _metrics(P, F)
    P2, F2 = clean_surface(
        P,
        F,
        target=target,
        taubin=taubin,
        min_faces=min_faces,
        remesh=remesh,
        seal=seal,
        decimate=decimate,
    )
    after = _metrics(P2, F2)
    meshio.write(
        os.path.join(out, name), meshio.Mesh(points=P2, cells=[meshio.CellBlock("triangle", F2)])
    )
    return (name, before, after)


def clean_surface(
    P, F, target=0.0, taubin=30, min_faces=1000, remesh=False, seal=True, decimate=0.0
):
    import pymeshlab

    # 1) dedup + drop tiny flying fragments
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(P.astype(np.float64), F.astype(np.int32)), "s")
    ms.meshing_remove_duplicate_vertices()
    ms.meshing_remove_duplicate_faces()
    ms.meshing_remove_unreferenced_vertices()
    if min_faces > 0:
        try:
            ms.meshing_remove_connected_component_by_face_number(mincomponentsize=int(min_faces))
        except Exception:
            pass
    m = ms.current_mesh()
    P, F = np.asarray(m.vertex_matrix()), np.asarray(m.face_matrix())

    # 2) seal each component → watertight, manifold, holes/craters filled
    if seal:
        try:
            P, F = _seal_per_component(P, F)
        except Exception as exc:
            print("   [warn] seal:", exc)

    # 3) de-spike / smooth (blends the seal patches; more iters = smoother)
    ms2 = pymeshlab.MeshSet()
    ms2.add_mesh(pymeshlab.Mesh(P.astype(np.float64), F.astype(np.int32)), "s")
    ms2.apply_coord_taubin_smoothing(stepsmoothnum=int(taubin))
    if 0.0 < decimate < 1.0:
        # Quadric edge-collapse to `decimate` * face count. Preserve topology,
        # boundary and normals so the surface stays watertight/manifold with
        # outward winding; planar quadric keeps flat caps (the crop face) crisp.
        try:
            ms2.meshing_decimation_quadric_edge_collapse(
                targetperc=float(decimate),
                preservetopology=True,
                preserveboundary=True,
                preservenormal=True,
                planarquadric=True,
                autoclean=True,
            )
        except Exception as exc:
            print("   [warn] decimate:", exc)
    if remesh:
        tl = pymeshlab.PureValue(target) if target > 0 else pymeshlab.PercentageValue(1.0)
        try:
            ms2.meshing_isotropic_explicit_remeshing(iterations=5, targetlen=tl, reprojectflag=True)
            ms2.apply_coord_taubin_smoothing(stepsmoothnum=10)
        except Exception as exc:
            print("   [warn] remesh:", exc)
    m2 = ms2.current_mesh()
    P3, F3 = np.asarray(m2.vertex_matrix()), np.asarray(m2.face_matrix())
    # Consistent OUTWARD winding — a surface/layer photon-MC keys reflection and
    # refraction off the face normal, so every surface must share one convention.
    try:
        import trimesh

        t = trimesh.Trimesh(vertices=P3, faces=F3, process=False)
        t.fix_normals()
        P3, F3 = np.asarray(t.vertices), np.asarray(t.faces)
    except Exception:
        pass
    return P3, F3


def main() -> int:
    ap = argparse.ArgumentParser(description="Polish extracted tissue surfaces")
    ap.add_argument("--dir", help="folder containing surface_*.vtk")
    ap.add_argument("--config", help="pipeline_config*.json (uses vtk_export/conformal)")
    ap.add_argument("--out", help="output folder (default: overwrite input)")
    ap.add_argument(
        "--min-faces", type=int, default=1000, help="drop components smaller than this (0=keep all)"
    )
    ap.add_argument(
        "--taubin", type=int, default=30, help="Taubin smoothing iterations (higher = smoother)"
    )
    ap.add_argument("--no-seal", action="store_true", help="skip pymeshfix watertight sealing")
    ap.add_argument(
        "--remesh", action="store_true", help="also isotropic-remesh (uniform triangles)"
    )
    ap.add_argument("--target", type=float, default=0.0, help="remesh target edge (mm; 0=auto)")
    ap.add_argument(
        "--decimate",
        type=float,
        default=0.0,
        help="quadric-decimate to this FRACTION of faces (0.5 = keep 50%%; "
        "0 = off). Preserves watertight/manifold/outward normals.",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="parallel files (0 = auto ~half the cores; surfaces are independent)",
    )
    args = ap.parse_args()

    if args.dir:
        src = args.dir
    elif args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        src = os.path.join(cfg["output_dir"], "vtk_export", "conformal")
    else:
        ap.error("need --dir or --config")
    out = args.out or src
    os.makedirs(out, exist_ok=True)

    surfs = sorted(glob.glob(os.path.join(src, "surface_*.vtk")))
    if not surfs:
        print(f"нет surface_*.vtk в {src}")
        return 1
    jobs = args.jobs or max(1, min(len(surfs), (os.cpu_count() or 4) // 2))
    print(f"Чистка {len(surfs)} поверхностей ({jobs} параллельно): {src}")
    jobargs = [
        (
            s,
            out,
            args.target,
            args.taubin,
            args.min_faces,
            args.remesh,
            not args.no_seal,
            args.decimate,
        )
        for s in surfs
    ]
    if jobs == 1:
        results = [_clean_file(j) for j in jobargs]
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        results = []
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for fut in as_completed([pool.submit(_clean_file, j) for j in jobargs]):
                results.append(fut.result())
    for name, before, after in sorted(results):
        if before is None:
            print(f"  {name}: нет треугольников, пропуск")
            continue
        print(f"  {name}\n    ДО:    {before}\n    ПОСЛЕ: {after}")
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
