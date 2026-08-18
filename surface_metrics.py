# -*- coding: utf-8 -*-
"""
surface_metrics.py — quality + fidelity metrics for the nested envelope surfaces.

For every surface_NN_<tissue>.vtk in a surfaces_envelopes dir it reports a broad
set of metrics, grouped as:

  geometry   vertices, faces, surface area (mm^2), enclosed volume (mm^3),
             bounding-box extents, edge length (mean/min/max), mean triangle area
  quality    watertight, genus, components, non-manifold edges, boundary holes,
             self-intersecting faces, triangle min/max angle, median min-angle,
             sliver %, aspect ratio (mean / p99 worst)
  fidelity   distance of the surface from
               (a) the VOXEL envelope boundary   -> true to the segmentation
                   (rms / mean / max=Hausdorff, mm)
               (b) the RAW CGAL mesh before clean -> cost of Taubin + decimation
                   (rms / mean / symmetric Hausdorff, mm; n/a unless a metrics run
                    saved the raw meshes under _metrics/raw)
  nesting    pokethrough % (vertices outside the parent shell), the tightest and
             mean clearance to the parent (min/mean gap, mm) and the max protrusion

(a) uses a padded EDT distance field of the tissue's envelope region rebuilt from
02_merged.npy (exact region if a metrics run saved it under _metrics/regions).

Writes metrics.json + metrics.csv + metrics.txt into the surfaces dir.

Usage:
    python surface_metrics.py --config pipeline_config_brain.json
Runs in the main env (numpy/scipy/vtk/pymeshlab/trimesh).
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys

import numpy as np
from scipy import ndimage as ndi

import vtk
from vtk.util.numpy_support import vtk_to_numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SLIVER_ANGLE_DEG = 10.0
_GAP_SAMPLES = 40000  # subsample for signed-distance (gap) stats


# ── IO ──────────────────────────────────────────────────────────────────────


def _read_surface(path):
    """Return (verts Nx3 float64, faces Mx3 int, polydata) from a VTK surface file."""
    r = vtk.vtkUnstructuredGridReader()
    r.SetFileName(path)
    r.Update()
    gf = vtk.vtkGeometryFilter()
    gf.SetInputConnection(r.GetOutputPort())
    gf.Update()
    tf = vtk.vtkTriangleFilter()
    tf.SetInputConnection(gf.GetOutputPort())
    tf.Update()
    poly = tf.GetOutput()
    verts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    fa = vtk_to_numpy(poly.GetPolys().GetData())
    faces = fa.reshape(-1, 4)[:, 1:4].astype(np.int64)
    return verts, faces, poly


# ── geometry + triangle quality (numpy) ──────────────────────────────────────


def _geometry(verts, faces):
    p = verts[faces]  # (M,3,3)
    v0, v1, v2 = p[:, 0], p[:, 1], p[:, 2]
    la = np.linalg.norm(v1 - v0, axis=1)
    lb = np.linalg.norm(v2 - v1, axis=1)
    lc = np.linalg.norm(v0 - v2, axis=1)
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    s = (la + lb + lc) / 2.0

    r_in = np.zeros_like(area)
    m = s > 0
    r_in[m] = area[m] / s[m]
    r_circ = np.full_like(area, np.inf)
    good = area > 1e-12
    r_circ[good] = (la[good] * lb[good] * lc[good]) / (4.0 * area[good])
    aspect = np.full_like(area, np.inf)
    ok = r_in > 1e-12
    aspect[ok] = r_circ[ok] / (2.0 * r_in[ok])  # 1 = equilateral, large = sliver
    aspect = aspect[np.isfinite(aspect)]

    def _ang(u, w):
        nu = np.linalg.norm(u, axis=1)
        nw = np.linalg.norm(w, axis=1)
        d = np.einsum("ij,ij->i", u, w) / np.clip(nu * nw, 1e-12, None)
        return np.degrees(np.arccos(np.clip(d, -1.0, 1.0)))

    a0 = _ang(v1 - v0, v2 - v0)
    a1 = _ang(v0 - v1, v2 - v1)
    a2 = 180.0 - a0 - a1
    amin = np.minimum(np.minimum(a0, a1), a2)
    amax = np.maximum(np.maximum(a0, a1), a2)
    edges = np.concatenate([la, lb, lc])
    return {
        "area_mm2": float(area.sum()),
        "tri_area_mean_mm2": float(area.mean()),
        "edge_mean_mm": float(edges.mean()),
        "edge_min_mm": float(edges.min()),
        "edge_max_mm": float(edges.max()),
        "bbox_x_mm": float(np.ptp(verts[:, 0])),
        "bbox_y_mm": float(np.ptp(verts[:, 1])),
        "bbox_z_mm": float(np.ptp(verts[:, 2])),
        "min_angle_deg": float(amin.min()),
        "max_angle_deg": float(amax.max()),
        "median_min_angle_deg": float(np.median(amin)),
        "sliver_pct": float(100.0 * np.mean(amin < SLIVER_ANGLE_DEG)),
        "aspect_mean": float(aspect.mean()) if aspect.size else -1.0,
        "aspect_p99": float(np.percentile(aspect, 99)) if aspect.size else -1.0,
    }


# ── topology via pymeshlab ────────────────────────────────────────────────────


def _topology(verts, faces):
    import pymeshlab

    q = {
        "watertight": None,
        "genus": None,
        "components": -1,
        "boundary_edges": -1,
        "holes": -1,
        "nonmanifold_edges": -1,
        "nonmanifold_vertices": -1,
        "self_intersections": -1,
        "volume_mm3": -1.0,
    }
    try:
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(verts, faces))
        try:
            geo = ms.get_geometric_measures()
            q["volume_mm3"] = abs(float(geo.get("mesh_volume", 0.0)))
        except Exception:
            pass
        try:
            topo = ms.get_topological_measures()
        except Exception:
            topo = {}
        q["components"] = int(topo.get("connected_components_number", -1))
        q["boundary_edges"] = int(topo.get("boundary_edges", -1))
        q["holes"] = int(topo.get("number_holes", -1))
        q["nonmanifold_edges"] = int(topo.get("non_two_manifold_edges", -1))
        q["nonmanifold_vertices"] = int(topo.get("non_two_manifold_vertices", -1))
        g = topo.get("genus", None)
        q["genus"] = int(g) if g is not None and g >= 0 else None
        q["watertight"] = bool(q["boundary_edges"] == 0 and q["nonmanifold_edges"] == 0)
        try:
            ms.compute_selection_by_self_intersections_per_face()
            q["self_intersections"] = int(ms.current_mesh().selected_face_number())
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        q["topology_error"] = str(exc)
    return q


# ── fidelity: distance field from the voxel envelope region ──────────────────


def _region_distance_field(region, spacing_zyx, pad=1):
    """mm distance from every voxel to the region boundary (unsigned). The region
    is padded with background so a crop-plane face (where the region is merely
    clipped by the array edge) becomes a real boundary — otherwise the flat cap
    vertices there would sample deep interior distances."""
    r = np.pad(region, pad, mode="constant", constant_values=False)
    din = ndi.distance_transform_edt(r, sampling=spacing_zyx)
    dout = ndi.distance_transform_edt(~r, sampling=spacing_zyx)
    return np.where(r, din, dout).astype(np.float32)


def _sample_field(field, verts, spacing_zyx, pad=1):
    """Trilinear-sample a padded (z,y,x) field at mesh verts
    (mesh X=z*dz, Y=y*dy, Z=x*dx; +pad for the padding offset)."""
    dz, dy, dx = spacing_zyx
    idx = np.vstack([verts[:, 0] / dz + pad, verts[:, 1] / dy + pad, verts[:, 2] / dx + pad])
    return ndi.map_coordinates(field, idx, order=1, mode="nearest")


def _dist_to_mesh(verts, ref_verts, ref_faces):
    """(rms, mean, max) distance from verts to the reference surface."""
    import trimesh

    ref = trimesh.Trimesh(vertices=ref_verts, faces=ref_faces, process=False)
    _, d, _ = trimesh.proximity.closest_point(ref, verts)
    return float(np.sqrt(np.mean(d**2))), float(np.mean(d)), float(np.max(d))


def _raw_from_mesh(mesh_path, name):
    """Extract the label-1 boundary surface from a raw CGAL .mesh (the surface
    before surface_cleaner's Taubin/decimation). Returns (verts, faces) or None."""
    import tempfile

    try:
        import mc_mesh_check

        with tempfile.TemporaryDirectory() as td:
            mc_mesh_check.export_surfaces(mesh_path, td, {1: name})
            vp = os.path.join(td, f"surface_01_{name}.vtk")
            if os.path.exists(vp):
                v, f, _ = _read_surface(vp)
                return v, f
    except Exception:
        pass
    return None


# ── nesting ──────────────────────────────────────────────────────────────────


def _nesting(child_poly, parent_poly):
    sel = vtk.vtkSelectEnclosedPoints()
    sel.SetInputData(child_poly)
    sel.SetSurfaceData(parent_poly)
    sel.SetTolerance(1e-8)
    sel.Update()
    inside = vtk_to_numpy(sel.GetOutput().GetPointData().GetArray("SelectedPoints")).astype(bool)
    pct_out = 100.0 * np.count_nonzero(~inside) / inside.size

    imp = vtk.vtkImplicitPolyDataDistance()
    imp.SetInput(parent_poly)
    cv = vtk_to_numpy(child_poly.GetPoints().GetData())
    step = max(1, len(cv) // _GAP_SAMPLES)
    signed = np.array([imp.EvaluateFunction(p) for p in cv[::step]])
    gap = -signed  # >0 = inside the parent by that much (clearance)
    return {
        "pokethrough_pct": float(pct_out),
        "min_gap_mm": float(gap.min()),
        "mean_gap_mm": float(gap.mean()),
        "max_protrude_mm": float(signed.max()),
    }


# ── driver ───────────────────────────────────────────────────────────────────


def compute(
    surf_dir, merged_npy, spacing_zyx, parents, names, raw_dir=None, region_dir=None, work_dir=None
):
    import build_envelopes as be

    vol = np.load(merged_npy)
    labels = sorted(int(l) for l in np.unique(vol) if l != 0)
    parent = {int(k): int(v) for k, v in parents.items()}
    for L in labels:
        parent.setdefault(L, 0)

    files = {}
    for path in glob.glob(os.path.join(surf_dir, "surface_*.vtk")):
        m = re.search(r"surface_(\d+)_", os.path.basename(path))
        if m:
            files[int(m.group(1))] = path

    polys = {}
    results = {}
    for L, path in sorted(files.items()):
        name = names.get(L, f"label_{L}")
        verts, faces, poly = _read_surface(path)
        polys[L] = poly
        rec = {"label": L, "name": name, "vertices": int(len(verts)), "faces": int(len(faces))}
        rec.update(_geometry(verts, faces))
        rec.update(_topology(verts, faces))

        # (a) fidelity vs voxel envelope. Prefer the EXACT meshed region saved by
        # a --keep-work run (_metrics/regions or _work/env_L.npy); else rebuild the
        # anatomical envelope from 02_merged.
        region = None
        for cand in (
            os.path.join(region_dir, f"env_{L}.npy") if region_dir else None,
            os.path.join(work_dir, f"env_{L}.npy") if work_dir else None,
        ):
            if cand and os.path.exists(cand):
                region = np.load(cand).astype(bool)
                break
        if region is None:
            region = be.envelope_region(vol, L, parent)
        field = _region_distance_field(region, spacing_zyx)
        dv = _sample_field(field, verts, spacing_zyx)
        rec["rms_vox_mm"] = float(np.sqrt(np.mean(dv**2)))
        rec["mean_vox_mm"] = float(np.mean(dv))
        rec["max_vox_mm"] = float(np.max(dv))
        del field, region

        # (b) fidelity vs raw CGAL mesh (before Taubin/decimation), symmetric
        # Hausdorff. Raw surface from _metrics/raw or extracted from _work/env_L.mesh.
        raw = None
        rawvtk = os.path.join(raw_dir, f"surface_{L:02d}_{name}.vtk") if raw_dir else None
        if rawvtk and os.path.exists(rawvtk):
            rv, rf, _ = _read_surface(rawvtk)
            raw = (rv, rf)
        elif work_dir and os.path.exists(os.path.join(work_dir, f"env_{L}.mesh")):
            raw = _raw_from_mesh(os.path.join(work_dir, f"env_{L}.mesh"), name)
        if raw is not None:
            rv, rf = raw
            rms_fr, mean_fr, max_fr = _dist_to_mesh(verts, rv, rf)
            _, _, max_rf = _dist_to_mesh(rv, verts, faces)
            rec["rms_raw_mm"] = rms_fr
            rec["mean_raw_mm"] = mean_fr
            rec["hausdorff_raw_mm"] = float(max(max_fr, max_rf))
        else:
            rec["rms_raw_mm"] = rec["mean_raw_mm"] = rec["hausdorff_raw_mm"] = None
        results[L] = rec

    for L, rec in results.items():
        p = parent.get(L, 0)
        rec["parent"] = p
        if p and p in polys:
            rec.update(_nesting(polys[L], polys[p]))
        else:
            rec.update(
                {
                    "pokethrough_pct": 0.0,
                    "min_gap_mm": None,
                    "mean_gap_mm": None,
                    "max_protrude_mm": 0.0,
                }
            )
    return [results[L] for L in sorted(results)]


# ── reports ───────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "label",
    "name",
    "parent",
    "vertices",
    "faces",
    "area_mm2",
    "volume_mm3",
    "bbox_x_mm",
    "bbox_y_mm",
    "bbox_z_mm",
    "edge_mean_mm",
    "edge_min_mm",
    "edge_max_mm",
    "tri_area_mean_mm2",
    "watertight",
    "genus",
    "components",
    "boundary_edges",
    "holes",
    "nonmanifold_edges",
    "nonmanifold_vertices",
    "self_intersections",
    "min_angle_deg",
    "max_angle_deg",
    "median_min_angle_deg",
    "sliver_pct",
    "aspect_mean",
    "aspect_p99",
    "rms_vox_mm",
    "mean_vox_mm",
    "max_vox_mm",
    "rms_raw_mm",
    "mean_raw_mm",
    "hausdorff_raw_mm",
    "pokethrough_pct",
    "min_gap_mm",
    "mean_gap_mm",
    "max_protrude_mm",
]


def write_reports(rows, surf_dir):
    jpath = os.path.join(surf_dir, "metrics.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    cpath = os.path.join(surf_dir, "metrics.csv")
    with open(cpath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def g(r, k, fmt="{}", na="n/a"):
        v = r.get(k)
        return na if v is None else fmt.format(v)

    lines = [
        "Surface metrics (envelope surfaces) - full set in metrics.csv / metrics.json",
        "=" * 118,
        "",
    ]
    lines.append(
        f"{'tissue':<13}{'faces':>8}{'area cm2':>9}{'vol cm3':>9}{'wt':>3}{'gen':>5}"
        f"{'cmp':>4}{'selfx':>6}{'asp99':>7}{'sliv%':>6}"
        f"{'RMSvox':>8}{'MAXvox':>8}{'RMSraw':>8}{'Hraw':>7}{'minGap':>8}{'poke%':>7}"
    )
    lines.append("-" * 118)
    for r in rows:
        area_cm2 = r.get("area_mm2", 0) / 100.0
        vol_cm3 = r.get("volume_mm3", 0) / 1000.0
        lines.append(
            f"{r['name'][:12]:<13}{r['faces']:>8}{area_cm2:>9.1f}{vol_cm3:>9.1f}"
            f"{('Y' if r.get('watertight') else 'n'):>3}{g(r,'genus'):>5}"
            f"{g(r,'components'):>4}{g(r,'self_intersections'):>6}"
            f"{g(r,'aspect_p99','{:.1f}'):>7}{g(r,'sliver_pct','{:.2f}'):>6}"
            f"{g(r,'rms_vox_mm','{:.3f}'):>8}{g(r,'max_vox_mm','{:.3f}'):>8}"
            f"{g(r,'rms_raw_mm','{:.3f}'):>8}{g(r,'hausdorff_raw_mm','{:.2f}'):>7}"
            f"{g(r,'min_gap_mm','{:.3f}'):>8}{g(r,'pokethrough_pct','{:.3f}'):>7}"
        )
    lines += [
        "",
        "RMSvox/MAXvox = mm to the voxel envelope boundary; RMSraw/Hraw = mm to the raw CGAL mesh",
        "(pre Taubin/decimation, symmetric Hausdorff); asp99 = 99th-pct triangle aspect ratio;",
        "minGap = tightest clearance to the parent shell; poke% = vertices outside the parent.",
        "",
    ]
    tpath = os.path.join(surf_dir, "metrics.txt")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return jpath, cpath, tpath, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="quality + fidelity metrics for envelope surfaces")
    ap.add_argument("--config", required=True, help="pipeline_config*.json")
    ap.add_argument("--dir", help="surfaces_envelopes dir (default from config output_dir)")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out_dir = cfg["output_dir"]
    surf_dir = args.dir or os.path.join(out_dir, "vtk_export", "conformal", "surfaces_envelopes")
    merged = os.path.join(out_dir, "02_merged.npy")
    if not os.path.exists(merged):
        print(f"[metrics] {merged} not found", file=sys.stderr)
        return 2

    from parse_info_txt import parse_info_txt

    info = parse_info_txt(cfg["info_txt"]) if cfg.get("info_txt") else None
    spacing = tuple(info.spacing_zyx) if info else (1.0, 1.0, 1.0)
    names = {int(k): v for k, v in (info.label_names.items() if info else {})}
    parents = cfg.get("envelope_parents", {})
    raw_dir = os.path.join(surf_dir, "_metrics", "raw")
    region_dir = os.path.join(surf_dir, "_metrics", "regions")
    work_dir = os.path.join(surf_dir, "_work")  # present after a --keep-work run

    rows = compute(
        surf_dir,
        merged,
        spacing,
        parents,
        names,
        raw_dir=raw_dir if os.path.isdir(raw_dir) else None,
        region_dir=region_dir if os.path.isdir(region_dir) else None,
        work_dir=work_dir if os.path.isdir(work_dir) else None,
    )
    jpath, cpath, tpath, text = write_reports(rows, surf_dir)
    print(text)
    print(f"\n[metrics] wrote {jpath}\n[metrics] wrote {cpath}\n[metrics] wrote {tpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
