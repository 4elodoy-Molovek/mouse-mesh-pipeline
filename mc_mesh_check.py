# -*- coding: utf-8 -*-
"""
Monte-Carlo suitability checker for the meshes produced by the pipeline.

Where meshValidator.py / validate_conformal_surfaces() only ask "is this
surface watertight?", this tool checks the properties that actually decide
whether a mesh can carry a Monte-Carlo photon-transport simulation (MMC/MCX
style), and reports a per-criterion verdict:

  Tetrahedral mesh (brain_full_conformal.mesh — the artifact MMC consumes)
    - positive Jacobian:   no inverted / degenerate tets
    - conformality:        every internal face shared by exactly two tets,
                           every boundary face by one (no T-junctions)
    - per-material seal:   each region's boundary is a closed 2-manifold
                           (photons cannot leak between tissues)
    - element quality:     sliver fraction (extreme aspect ratio breaks tracing)
    - materials & units:   region id -> tissue name, geometry in mm

  Per-tissue surfaces (surface_*.vtk / conformal_*.vtk)
    - manifold + watertight, genus (Euler), component count,
      degenerate faces, outward orientation (positive enclosed volume)

Optional outputs:
    --export-mmc   write tetgen .node/.elem (1-indexed, region tag) next to
                   the .mesh — the format MMC / iso2mesh read directly.
    --props        write an optical-property template CSV
                   (label, name, mua[1/mm], mus[1/mm], g, n) for the regions
                   actually present, ready to be filled in for the solver.

Usage:
    python mc_mesh_check.py --config pipeline_config_mouse.json
    python mc_mesh_check.py --mesh path/to/brain_full_conformal.mesh --export-mmc
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import meshio  # type: ignore

    _HAS_MESHIO = True
except ImportError:  # pragma: no cover
    _HAS_MESHIO = False

# Faces of a tetra as local vertex indices.
_TET_FACES = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.intp)
_SLIVER_Q = 0.01  # normalised quality below this is treated as a sliver


# ── low-level geometry ────────────────────────────────────────────────────


def _signed_vol6(pts: np.ndarray, tet: np.ndarray) -> np.ndarray:
    a, b, c, d = pts[tet[:, 0]], pts[tet[:, 1]], pts[tet[:, 2]], pts[tet[:, 3]]
    return np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)


def _tet_quality(pts: np.ndarray, tet: np.ndarray) -> np.ndarray:
    """Normalised shape quality; 1.0 for a regular tet, ->0 for a sliver."""
    vol = np.abs(_signed_vol6(pts, tet)) / 6.0
    el2 = np.zeros(len(tet))
    for i, j in [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]:
        el2 += np.sum((pts[tet[:, i]] - pts[tet[:, j]]) ** 2, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = 12.0 * (3.0 * vol) ** (2.0 / 3.0) / el2
    return np.nan_to_num(q)


def _edge_defects(faces: np.ndarray) -> Tuple[int, int]:
    """Classify boundary-edge defects of a triangle set.

    Returns (n_holes, n_nonmanifold):
      n_holes       — edges used an odd number of times → a genuine gap/leak;
      n_nonmanifold — edges used an even number > 2 → self-touching boundary.
    A closed 2-manifold has both counts == 0.
    """
    if len(faces) == 0:
        return 0, 0
    e = np.sort(
        np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [0, 2]]]),
        axis=1,
    )
    _, counts = np.unique(e, axis=0, return_counts=True)
    n_holes = int(np.sum(counts % 2 == 1))
    n_nonmanifold = int(np.sum((counts % 2 == 0) & (counts != 2)))
    return n_holes, n_nonmanifold


def _n_components(tri: np.ndarray, n_pts: int) -> int:
    """Connected-component count of a triangle surface (via vertex graph)."""
    import scipy.sparse as sp
    import scipy.sparse.csgraph as csg

    e = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [0, 2]]])
    r = np.concatenate([e[:, 0], e[:, 1]])
    c = np.concatenate([e[:, 1], e[:, 0]])
    used = np.unique(tri)
    adj = sp.csr_matrix((np.ones(len(r)), (r, c)), shape=(n_pts, n_pts))
    n_all, lab = csg.connected_components(adj, directed=False)
    return int(len(np.unique(lab[used])))


# ── report helper ─────────────────────────────────────────────────────────


class _Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.blockers = 0
        self.warnings = 0

    def h(self, text: str) -> None:
        self.lines += ["", "=" * 78, text, "=" * 78]

    def p(self, text: str = "") -> None:
        self.lines.append(text)

    def verdict(self, label: str, ok: bool, detail: str, blocker: bool = True) -> None:
        if ok:
            tag = "OK     "
        elif blocker:
            tag = "BLOCKER"
            self.blockers += 1
        else:
            tag = "WARN   "
            self.warnings += 1
        self.p(f"  [{tag}] {label:<26} {detail}")


# ── tet-mesh checks ───────────────────────────────────────────────────────


def _load_tet(path: str, true_labels: Optional[List[int]] = None):
    """Load all tetrahedra + their medit:ref region ids from a .mesh file.

    Two pitfalls this guards against (both silently produced wrong results
    before this fix — mirrors the handling in npy2conformal_mesh's
    _export_surface_from_tet, which already gets this right):

    1. meshio splits a multi-region medit tet mesh into ONE CellBlock per
       region, each with its own "medit:ref" entry — not a single block.
       Reading only the last block silently drops every other region's tets.
    2. CGAL/pygalmesh renumbers subdomains sequentially by sorted rank of the
       input label values (e.g. true labels [1,2,4,5,6,7] -> refs [1..6]),
       so a "hole" in the label numbering (label 3 missing here) makes refs
       diverge from true tissue labels. When the caller supplies the true
       label set (from INFO.txt) and its size matches, remap refs back.

    Returns (points, tet, ref, remap) where remap is the ref->label dict
    that was applied, or None if no remap was needed/possible.
    """
    m = meshio.read(path)
    refs_by_block = m.cell_data.get("medit:ref")
    tet_parts = [
        (cb.data, ref)
        for cb, ref in zip(m.cells, refs_by_block or [])
        if cb.type == "tetra" and len(ref)
    ]
    if not tet_parts:
        return m.points, None, None, None

    tet = np.vstack([td for td, _ in tet_parts])
    ref = np.concatenate([r for _, r in tet_parts]).ravel()

    remap = None
    if true_labels:
        present_refs = sorted({int(r) for r in np.unique(ref)})
        sorted_labels = sorted({int(l) for l in true_labels})
        if len(present_refs) == len(sorted_labels) and present_refs != sorted_labels:
            remap = dict(zip(present_refs, sorted_labels))
            lut = np.arange(max(present_refs) + 1, dtype=ref.dtype)
            for r, l in remap.items():
                lut[r] = l
            ref = lut[ref]

    return m.points, tet, ref, remap


def check_tet_mesh(
    path: str, names: Optional[Dict[int, str]], rep: _Report
) -> Optional[np.ndarray]:
    rep.h(f"TETRAHEDRAL MESH (MMC input)\n{path}")
    true_labels = sorted(names.keys()) if names else None
    pts, tet, ref, remap = _load_tet(path, true_labels)
    if remap:
        rep.p(f"  (CGAL subdomain remap applied: {remap})")
    if tet is None:
        rep.verdict("tetra block", False, "no tetrahedra found in file")
        return None

    bb0, bb1 = pts.min(0), pts.max(0)
    rep.p(
        f"  points={len(pts)}  tets={len(tet)}  "
        f"bbox(mm)=[{bb0[0]:.1f},{bb0[1]:.1f},{bb0[2]:.1f}].."
        f"[{bb1[0]:.1f},{bb1[1]:.1f},{bb1[2]:.1f}]"
    )

    # 1. positive Jacobian
    v6 = _signed_vol6(pts, tet)
    n_inv = int(np.sum(v6 < 0))
    n_deg = int(np.sum(np.abs(v6) < 1e-12))
    # allow a consistent negative orientation (all tets same sign) — that is
    # just a global winding, not an inversion.
    consistent = (n_inv == 0) or (n_inv == len(tet))
    rep.verdict(
        "positive Jacobian",
        n_deg == 0 and consistent,
        f"{n_inv} inverted, {n_deg} degenerate of {len(tet)}",
    )

    # 2. conformality: no face shared by >2 tets
    faces = np.sort(tet[:, _TET_FACES].reshape(-1, 3), axis=1)
    _, fcounts = np.unique(faces, axis=0, return_counts=True)
    n_over = int(np.sum(fcounts > 2))
    n_bnd = int(np.sum(fcounts == 1))
    rep.verdict(
        "conformal (<=2 tets/face)",
        n_over == 0,
        f"{n_over} faces shared by >2 tets; {n_bnd} boundary faces",
    )

    # 3. element quality
    q = _tet_quality(pts, tet)
    n_sliver = int(np.sum(q < _SLIVER_Q))
    rep.verdict(
        "element quality",
        n_sliver == 0,
        f"{n_sliver} slivers (q<{_SLIVER_Q}); q_min={q.min():.4f} " f"q_med={np.median(q):.3f}",
        blocker=False,
    )

    # 4. per-material seal
    if ref is None:
        rep.verdict("per-material seal", False, "no medit:ref labels", blocker=False)
    else:
        regs = sorted(int(r) for r in np.unique(ref))
        leaky, nonmanifold = [], []
        for r in regs:
            rtet = tet[ref == r]
            rf = np.sort(rtet[:, _TET_FACES].reshape(-1, 3), axis=1)
            uf, c = np.unique(rf, axis=0, return_counts=True)
            bnd = uf[c == 1]
            n_holes, n_nm = _edge_defects(bnd)
            nm = (names or {}).get(r, f"label_{r}")
            if n_holes:
                leaky.append(f"{r}:{nm}({n_holes} holes)")
            if n_nm:
                nonmanifold.append(f"{r}:{nm}({n_nm})")
        # A true leak (odd edges) is a blocker; non-manifold boundary contact is
        # a warning — photons don't leak, but the interface is not a clean 2-manifold.
        rep.verdict(
            "per-material seal (holes)",
            not leaky,
            "all regions closed" if not leaky else "leaky: " + ", ".join(leaky),
        )
        rep.verdict(
            "per-material manifold",
            not nonmanifold,
            (
                "all boundaries 2-manifold"
                if not nonmanifold
                else "non-manifold edges in " + ", ".join(nonmanifold)
            ),
            blocker=False,
        )
        # material table
        rep.p("  materials (region id -> tissue, tets):")
        for r in regs:
            nm = (names or {}).get(r, f"label_{r}")
            rep.p(f"      {r:>3}  {nm:<20} {int(np.sum(ref == r)):>8} tets")
    return ref


# ── surface checks ────────────────────────────────────────────────────────


def _load_surface(path: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    m = meshio.read(path)
    tri = next((np.asarray(cb.data) for cb in m.cells if cb.type == "triangle"), None)
    if tri is None or len(m.points) == 0:
        return None
    return m.points, tri


def check_surface(path: str, rep: _Report) -> None:
    name = os.path.basename(path)
    loaded = _load_surface(path)
    if loaded is None:
        rep.verdict(name, False, "no triangles", blocker=False)
        return
    pts, tri = loaded

    # drop degenerate (zero-area) faces from the topology counts
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    area2 = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    n_degen = int(np.sum(area2 < 1e-12))

    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [0, 2]]]), axis=1)
    ue, ec = np.unique(e, axis=0, return_counts=True)
    n_nonmanifold = int(np.sum(ec > 2))
    n_boundary = int(np.sum(ec == 1))
    watertight = n_nonmanifold == 0 and n_boundary == 0

    ncomp = _n_components(tri, len(pts))
    euler = len(np.unique(tri)) - len(ue) + len(tri)  # V - E + F on used verts
    vol6 = np.sum(np.einsum("ij,ij->i", np.cross(b, c), a))  # 6*signed volume
    # euler = 2*ncomp - 2*genus_total for closed manifolds
    genus = (2 * ncomp - euler) // 2 if watertight else None

    gtxt = f"genus={genus}" if genus is not None else f"open({n_boundary} edges)"
    detail = (
        f"wt={watertight} comp={ncomp} euler={euler} {gtxt} "
        f"nonmanifold_edges={n_nonmanifold} degen_faces={n_degen} "
        f"orient={'out' if vol6 > 0 else 'in'}"
    )
    # A surface is MC-usable if watertight, single-component, genus 0, outward,
    # with no non-manifold or degenerate elements.
    ok = (
        watertight
        and n_nonmanifold == 0
        and ncomp == 1
        and genus == 0
        and vol6 > 0
        and n_degen == 0
    )
    rep.verdict(name, ok, detail, blocker=False)


# ── MMC export & optical template ─────────────────────────────────────────


def export_mmc(
    path: str, out_prefix: str, names: Optional[Dict[int, str]] = None
) -> Tuple[str, str]:
    """Write tetgen .node/.elem (1-indexed, region tag) that MMC/iso2mesh read."""
    true_labels = sorted(names.keys()) if names else None
    pts, tet, ref, _remap = _load_tet(path, true_labels)
    if tet is None:
        raise ValueError("no tetrahedra to export")
    # ensure positive orientation for every element (MMC expects it)
    v6 = _signed_vol6(pts, tet)
    flip = v6 < 0
    if flip.any():
        tet = tet.copy()
        tet[flip] = tet[flip][:, [0, 1, 3, 2]]
    if ref is None:
        ref = np.ones(len(tet), dtype=np.int64)

    node_path = out_prefix + ".node"
    elem_path = out_prefix + ".elem"
    with open(node_path, "w", encoding="ascii") as f:
        f.write(f"{len(pts)} 3 0 0\n")
        for i, (x, y, z) in enumerate(pts, start=1):
            f.write(f"{i} {x:.6f} {y:.6f} {z:.6f}\n")
    with open(elem_path, "w", encoding="ascii") as f:
        f.write(f"{len(tet)} 4 1\n")
        for i, (a, b, c, d) in enumerate(tet + 1, start=1):
            f.write(f"{i} {a} {b} {c} {d} {int(ref[i - 1])}\n")
    return node_path, elem_path


def export_surfaces(path: str, out_dir: str, names: Optional[Dict[int, str]] = None) -> List[str]:
    """Write one watertight per-tissue surface VTK (boundary faces of each
    region's tets), with true tissue labels in the file names. For inspection
    in ParaView and for surface-based MC (MCX). Returns the written paths."""
    true_labels = sorted(names.keys()) if names else None
    pts, tet, ref, _remap = _load_tet(path, true_labels)
    if tet is None or ref is None:
        return []
    os.makedirs(out_dir, exist_ok=True)
    saved: List[str] = []
    for lbl in sorted(int(r) for r in np.unique(ref)):
        rtet = tet[ref == lbl]
        raw = rtet[:, _TET_FACES].reshape(-1, 3)  # keep winding
        srt = np.sort(raw, axis=1)
        _, first, counts = np.unique(srt, axis=0, return_index=True, return_counts=True)
        bnd = raw[first[counts == 1]]  # boundary faces, original winding
        if len(bnd) == 0:
            continue
        used = np.unique(bnd)
        remap = np.zeros(len(pts), dtype=np.int64)
        remap[used] = np.arange(len(used))
        nm = (names or {}).get(lbl, f"label_{lbl:02d}")
        out = os.path.join(out_dir, f"surface_{lbl:02d}_{nm}.vtk")
        meshio.write(
            out, meshio.Mesh(points=pts[used], cells=[meshio.CellBlock("triangle", remap[bnd])])
        )
        saved.append(out)
    return saved


def write_props_template(
    path: str, ref: Optional[np.ndarray], names: Optional[Dict[int, str]], out_csv: str
) -> None:
    import csv

    if ref is None:
        true_labels = sorted(names.keys()) if names else None
        _, _, ref, _remap = _load_tet(path, true_labels)
    regs = sorted(int(r) for r in np.unique(ref)) if ref is not None else []
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "name", "mua_1/mm", "mus_1/mm", "g", "n"])
        for r in regs:
            w.writerow([r, (names or {}).get(r, f"label_{r}"), "", "", "", ""])


# ── driver ────────────────────────────────────────────────────────────────


def _label_names_from_config(cfg: dict) -> Optional[Dict[int, str]]:
    info_txt = cfg.get("info_txt")
    if info_txt and os.path.exists(info_txt):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from parse_info_txt import parse_info_txt

        return parse_info_txt(info_txt).label_names
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Monte-Carlo suitability checker")
    ap.add_argument("--config", default="pipeline_config.json")
    ap.add_argument("--mesh", help="explicit .mesh tet file (overrides --config)")
    ap.add_argument("--surfaces", help="dir of surface_*.vtk (overrides --config)")
    ap.add_argument("--export-mmc", action="store_true", help="write tetgen .node/.elem")
    ap.add_argument(
        "--export-surfaces",
        nargs="?",
        const="__default__",
        help="write per-tissue surface VTKs (default: <mesh dir>/surfaces)",
    )
    ap.add_argument(
        "--props", nargs="?", const="__default__", help="write optical-property template CSV"
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when the tet mesh has MC blockers "
        "(default: report-only, exit 0 so the pipeline continues)",
    )
    args = ap.parse_args()

    if not _HAS_MESHIO:
        print("mc_mesh_check requires meshio (pip install meshio)")
        return 2

    names: Optional[Dict[int, str]] = None
    conformal_dir = None
    # Load tissue names from the config whenever it exists — needed both for
    # correct tissue naming AND for the CGAL subdomain->true-label remap in
    # _load_tet. This must happen even when an explicit --mesh is given (e.g.
    # validating a stray .mesh from cgal_remesh), not only in the config-driven
    # path below — otherwise surfaces come out as label_N with unremapped ids.
    cfg: Optional[dict] = None
    if args.config and os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        names = _label_names_from_config(cfg)
    if args.mesh or args.surfaces:
        mesh_path = args.mesh
        conformal_dir = args.surfaces or (os.path.dirname(mesh_path) if mesh_path else None)
    else:
        if cfg is None:
            raise SystemExit(f"config not found: {args.config}")
        conformal_dir = os.path.join(cfg["output_dir"], "vtk_export", "conformal")
        mesh_path = os.path.join(conformal_dir, "brain_full_conformal.mesh")

    rep = _Report()
    rep.p("MONTE-CARLO MESH SUITABILITY REPORT")

    ref = None
    if mesh_path and os.path.exists(mesh_path):
        ref = check_tet_mesh(mesh_path, names, rep)
    else:
        rep.h("TETRAHEDRAL MESH")
        rep.p(f"  (not found: {mesh_path})")

    if conformal_dir and os.path.isdir(conformal_dir):
        surfs = sorted(
            glob.glob(os.path.join(conformal_dir, "surface_*.vtk"))
            + glob.glob(os.path.join(conformal_dir, "conformal_*.vtk"))
        )
        if surfs:
            rep.h("PER-TISSUE SURFACES")
            for s in surfs:
                check_surface(s, rep)

    if args.export_mmc and mesh_path and os.path.exists(mesh_path):
        node_p, elem_p = export_mmc(mesh_path, os.path.splitext(mesh_path)[0], names)
        rep.h("MMC EXPORT")
        rep.p(f"  wrote {node_p}")
        rep.p(f"  wrote {elem_p}")

    if args.export_surfaces and mesh_path and os.path.exists(mesh_path):
        surf_dir = (
            os.path.join(os.path.dirname(mesh_path), "surfaces")
            if args.export_surfaces == "__default__"
            else args.export_surfaces
        )
        saved = export_surfaces(mesh_path, surf_dir, names)
        rep.h("SURFACE EXPORT")
        rep.p(f"  wrote {len(saved)} surfaces to {surf_dir}")

    if args.props and mesh_path and os.path.exists(mesh_path):
        out_csv = (
            os.path.join(os.path.dirname(mesh_path), "optical_properties.csv")
            if args.props == "__default__"
            else args.props
        )
        write_props_template(mesh_path, ref, names, out_csv)
        rep.h("OPTICAL PROPERTY TEMPLATE")
        rep.p(f"  wrote {out_csv}  (fill in mua/mus/g/n per label)")

    rep.h("SUMMARY")
    rep.p(f"  blockers={rep.blockers}  warnings={rep.warnings}")
    rep.p(
        "  VERDICT: "
        + ("NOT MC-READY (see BLOCKER lines)" if rep.blockers else "MC-READY (tet mesh)")
    )

    text = "\n".join(rep.lines)
    print(text)
    if mesh_path:
        out = os.path.join(os.path.dirname(mesh_path) or ".", "mc_report.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[report written] {out}")
    # Report-only by default: a validator step must not halt the pipeline (e.g.
    # before surface_cleaner). Use --strict to make blockers a non-zero exit.
    return 1 if (args.strict and rep.blockers) else 0


if __name__ == "__main__":
    raise SystemExit(main())
