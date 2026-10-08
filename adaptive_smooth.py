# -*- coding: utf-8 -*-
"""
adaptive_smooth.py — feature-preserving, artifact-targeted mesh smoothing.

WHY (and how this differs from the plain Taubin pass in surface_cleaner):
    Uniform Taubin smooths everything equally. On a convoluted cortex that is
    harmful twice over: it blurs genuine sulci while barely taming the isolated
    spikes that aggressive quadric decimation leaves behind (we measured 8-15 mm
    outliers when pushing a brain surface toward ~100k faces).

    This module follows the principle of staircase/context-aware smoothing of
    medical surface meshes (Muench, Adler, Preim, VCBM 2010; Moench et al.,
    Computers & Graphics 2011): instead of one global strength, derive a
    PER-VERTEX weight from an artifact detector and smooth non-uniformly. The
    detector here is adapted to the artifacts our pipeline actually produces,
    which are decimation spikes rather than slice-axis terracing (CGAL Mesh_3
    with a facet_distance bound does not generate staircases).

DETECTOR (spike vs. genuine feature):
    Let d(v) = centroid(N(v)) - v be the umbrella (uniform Laplacian) vector and
    h(v) the mean incident edge length. The dimensionless roughness

        s(v) = |d(v)| / h(v)

    is near zero on a smooth patch and grows with local relief. Roughness alone
    cannot separate a spike from a real ridge, so we compare a vertex with its
    own neighbourhood:

        a(v) = s(v) / (mean_{u in N(v)} s(u) + eps)

    A genuine ridge or sulcus is COHERENT: neighbours are displaced similarly,
    so a(v) ~ 1. An isolated spike pulls its own umbrella fully but each
    neighbour only by about 1/deg of that, so a(v) ~ deg (typically 5-7). The
    weight ramps between two thresholds on a(v), keeping a small floor so the
    whole surface still receives a gentle polish:

        w(v) = w_min + (1 - w_min) * smoothstep(a_lo, a_hi, a(v))

OPTIONAL REFERENCE TERM:
    When the voxel region the surface was built from is available (env_L.npy
    kept by build_envelopes --keep-work), vertices far from the true boundary
    are artifacts by definition. Their weight is raised in proportion to
    |signed distance| measured in voxels, which pulls the mesh back toward the
    reference instead of merely making it smooth.

Topology is never modified (only vertex positions move), so watertightness and
manifoldness are preserved. Run this BEFORE nest_repair: smoothing can move a
child surface outward, and nest_repair is what guarantees strict nesting.

USAGE:
    python adaptive_smooth.py --dir <folder with surface_*.vtk>
    python adaptive_smooth.py --dir HQ --iters 40 --w-min 0.1 --a-hi 5
    python adaptive_smooth.py --dir HQ --report        # detector stats, no write
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# -- mesh helpers -----------------------------------------------------------


def _adjacency(faces, n_verts):
    """Sparse vertex-vertex adjacency (CSR) plus the degree of every vertex."""
    from scipy import sparse

    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.vstack([e, e[:, ::-1]])
    data = np.ones(len(e), dtype=np.float32)
    A = sparse.coo_matrix((data, (e[:, 0], e[:, 1])), shape=(n_verts, n_verts)).tocsr()
    A.data[:] = 1.0  # collapse duplicate edges
    deg = np.asarray(A.sum(axis=1)).ravel()
    deg[deg == 0] = 1.0
    return A, deg


def umbrella(verts, A, deg):
    """Uniform Laplacian vector d(v) = centroid(N(v)) - v."""
    return (A @ verts) / deg[:, None] - verts


def _mean_edge_length(verts, A, deg):
    """Mean incident edge length per vertex, used to make roughness scale free."""
    n = len(verts)
    tot = np.zeros(n)
    Acoo = A.tocoo()
    seg = np.linalg.norm(verts[Acoo.row] - verts[Acoo.col], axis=1)
    np.add.at(tot, Acoo.row, seg)
    h = tot / deg
    h[h <= 0] = np.finfo(float).eps
    return h


def roughness(verts, faces):
    """Dimensionless local roughness s(v) = |umbrella| / mean incident edge."""
    A, deg = _adjacency(faces, len(verts))
    d = umbrella(verts, A, deg)
    h = _mean_edge_length(verts, A, deg)
    return np.linalg.norm(d, axis=1) / h, A, deg


def _smoothstep(lo, hi, x):
    t = np.clip((x - lo) / max(hi - lo, 1e-12), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def artifact_weights(verts, faces, w_min=0.15, a_lo=1.5, a_hi=4.0, s_floor=1e-3):
    """Per-vertex smoothing weight in [w_min, 1] from the spike detector.

    Vertices whose roughness stands out against their own neighbourhood (an
    isolated spike) approach 1; coherent relief such as a sulcus stays at the
    w_min floor and is therefore preserved.
    """
    s, A, deg = roughness(verts, faces)
    s_nb = (A @ s) / deg  # mean roughness over the neighbourhood
    a = s / (s_nb + 1e-9)
    w = w_min + (1.0 - w_min) * _smoothstep(a_lo, a_hi, a)
    w[s < s_floor] = w_min  # already flat: nothing to fix
    return w, s, a


def reference_boost(verts, weights, field, spacing_zyx, pad=1, vox_tol=1.0):
    """Raise weights where the surface strays from its voxel reference.

    `field` is a signed distance field of the source region in voxel index space
    (as produced by surface_metrics._region_distance_field). Deviation beyond
    `vox_tol` voxels ramps the weight toward 1.
    """
    dz, dy, dx = spacing_zyx
    idx = np.stack(
        [
            verts[:, 0] / dz + pad,
            verts[:, 1] / dy + pad,
            verts[:, 2] / dx + pad,
        ],
        axis=0,
    )
    from scipy import ndimage as ndi

    dist = np.abs(ndi.map_coordinates(field, idx, order=1, mode="nearest"))
    boost = _smoothstep(vox_tol, 3.0 * vox_tol, dist)
    return np.maximum(weights, boost)


def adaptive_taubin(verts, faces, weights, iters=30, lam=0.5, mu=-0.53):
    """Taubin lambda/mu smoothing with a per-vertex strength.

    The alternating positive/negative steps keep the volume (that is the point
    of Taubin over plain Laplacian); scaling each step by w(v) localises the
    effect to the detected artifacts.
    """
    A, deg = _adjacency(faces, len(verts))
    V = verts.astype(np.float64).copy()
    w = weights[:, None]
    for _ in range(int(iters)):
        V += w * lam * ((A @ V) / deg[:, None] - V)
        V += w * mu * ((A @ V) / deg[:, None] - V)
    return V


def smooth_surface(
    verts,
    faces,
    iters=30,
    w_min=0.15,
    a_lo=1.5,
    a_hi=4.0,
    lam=0.5,
    mu=-0.53,
    field=None,
    spacing_zyx=(1.0, 1.0, 1.0),
    vox_tol=1.0,
):
    """Detect artifacts, build weights and smooth. Returns (verts, stats)."""
    w, s, a = artifact_weights(verts, faces, w_min=w_min, a_lo=a_lo, a_hi=a_hi)
    if field is not None:
        w = reference_boost(verts, w, field, spacing_zyx, vox_tol=vox_tol)
    out = adaptive_taubin(verts, faces, w, iters=iters, lam=lam, mu=mu)
    moved = np.linalg.norm(out - verts, axis=1)
    stats = {
        "vertices": int(len(verts)),
        "targeted_pct": float(100.0 * np.mean(w > w_min + 0.25 * (1.0 - w_min))),
        "weight_mean": float(w.mean()),
        "roughness_p99": float(np.percentile(s, 99)),
        "ratio_max": float(a.max()),
        "moved_max_mm": float(moved.max()),
        "moved_mean_mm": float(moved.mean()),
    }
    return out, stats


# -- IO ---------------------------------------------------------------------


def _read(path):
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

    r = vtk.vtkUnstructuredGridReader()
    r.SetFileName(path)
    r.Update()
    gf = vtk.vtkGeometryFilter()
    gf.SetInputData(r.GetOutput())
    gf.Update()
    poly = gf.GetOutput()
    verts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    pd = vtk_to_numpy(poly.GetPolys().GetData())
    faces = pd.reshape(-1, 4)[:, 1:] if pd.size and pd[0] == 3 else np.empty((0, 3), int)
    return verts, faces


def _write(path, verts, faces):
    import meshio

    mesh = meshio.Mesh(points=verts, cells=[meshio.CellBlock("triangle", faces)])
    try:
        from meshio.vtk import _vtk_42

        _vtk_42.write(path, mesh, binary=True)
    except Exception:  # pragma: no cover
        meshio.write(path, mesh)


def main() -> int:
    ap = argparse.ArgumentParser(description="artifact-targeted adaptive mesh smoothing")
    ap.add_argument("--dir", required=True, help="folder with surface_*.vtk")
    ap.add_argument("--out", help="output folder (default: in place)")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--w-min", type=float, default=0.15, help="baseline strength for clean areas")
    ap.add_argument("--a-lo", type=float, default=1.5, help="ratio where targeting starts")
    ap.add_argument("--a-hi", type=float, default=4.0, help="ratio of full strength")
    ap.add_argument("--report", action="store_true", help="only print detector stats")
    args = ap.parse_args()

    out_dir = args.out or args.dir
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(args.dir, "surface_*.vtk")))
    if not files:
        print(f"[adaptive_smooth] no surface_*.vtk in {args.dir}", file=sys.stderr)
        return 2

    print(f"{'surface':34s} {'verts':>8s} {'target%':>8s} {'max move':>9s} {'mean move':>10s}")
    for f in files:
        verts, faces = _read(f)
        if len(faces) == 0:
            print(f"{os.path.basename(f):34s}  no triangles, skipped")
            continue
        new, st = smooth_surface(
            verts, faces, iters=args.iters, w_min=args.w_min, a_lo=args.a_lo, a_hi=args.a_hi
        )
        print(
            f"{os.path.basename(f):34s} {st['vertices']:8d} {st['targeted_pct']:7.2f}% "
            f"{st['moved_max_mm']:8.3f}mm {st['moved_mean_mm']:9.4f}mm"
        )
        if not args.report:
            _write(os.path.join(out_dir, os.path.basename(f)), new, faces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
