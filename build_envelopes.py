# -*- coding: utf-8 -*-
"""
build_envelopes.py — build nested per-tissue OUTER-ENVELOPE surfaces for the
user's surface-based Monte Carlo (photonMove.cpp / mcml_intersection.cpp).

WHY ENVELOPES (not full tissue boundaries):
    photonMove.cpp::CrossBoundary calls
        FindIntersectionLayer(input, intersection->surfaceId, layerId)
    with ONLY (surfaceId, current layerId) — no position, no normal. So the
    layer on the far side of a surface must be uniquely determined by that pair,
    which is only possible under STRICT NESTING where each surface is a tissue's
    OUTER envelope (air|skin, skin|skull, skull|brain, ...). The old per-tissue
    FULL boundary (outer wall + inner walls around nested organs) breaks this:
    the skin surface faces air on one part and skull on another, and the
    skin|skull interface is represented twice. See surfaces_envelopes/README.md
    and the project memory 'project-surface-mc-contract'.

PIPELINE (per tissue T, outer->inner):
    region = binary_fill_holes(union(T, descendants(T)))   # solid, no inner walls
      -> npy2inr (cgal_env)  -> mesh_and_remesh --no-remesh  -> export label-1 surface
      -> surface_cleaner (Taubin + consistent OUTWARD normals)

Nesting is auto-derived by containment: tissue B is inside tissue A when most of
B's voxels fall within fill_holes(A). Each tissue's parent is the tightest such
container; air (0) is the root. Works for the mouse atlas (skin>skull>brain) and
generalises to the brain atlas.

USAGE:
    python build_envelopes.py --config pipeline_config_mouse.json
    python build_envelopes.py --config ... --facet-size 0.10 --facet-distance 0.05
    python build_envelopes.py --config ... --voxel-only        # just write region .npy

Runs in the MAIN env (numpy/scipy/meshio/trimesh). Shells out to the cgal_env
python for npy2inr and to mesh_and_remesh.exe (native, needs MSYS2 ucrt64 DLLs).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ── nesting ────────────────────────────────────────────────────────────────


def _outside_air(vol: np.ndarray, wall: int) -> np.ndarray:
    """Voxels reachable from the true outside (border AIR) while treating tissue
    `wall` as an impassable barrier. Robust to the crop plane: only air voxels on
    the array border seed the flood, so internal tissue flush with the crop face
    is NOT counted as outside."""
    free = vol != wall
    seed = np.zeros_like(free)
    seed[0, :, :] = seed[-1, :, :] = True
    seed[:, 0, :] = seed[:, -1, :] = True
    seed[:, :, 0] = seed[:, :, -1] = True
    seed &= vol == 0  # only genuine border air seeds
    lab, _ = ndi.label(free)
    comps = set(int(c) for c in np.unique(lab[seed]) if c != 0)
    return np.isin(lab, list(comps)) if comps else np.zeros_like(free)


def derive_nesting(vol: np.ndarray, labels: list[int]) -> dict[int, int]:
    """Return parent[label] (0 = air/root). B is inside A when A's shell blocks
    the outside-air flood from reaching B (>50% of B enclosed). The parent is the
    tightest container (smallest enclosed volume)."""
    outside = {A: _outside_air(vol, A) for A in labels}
    enclosed = {A: int((~outside[A]).sum()) for A in labels}
    parent: dict[int, int] = {}
    for L in labels:
        mask = vol == L
        n = int(mask.sum()) or 1
        cands = [
            A
            for A in labels
            if A != L and int((mask & ~outside[A]).sum()) / n > 0.5 and enclosed[A] > enclosed[L]
        ]
        parent[L] = min(cands, key=lambda A: enclosed[A]) if cands else 0
    return parent


def descendants(parent: dict[int, int], root: int) -> list[int]:
    """All labels transitively nested inside `root`."""
    out, stack = [], [k for k, p in parent.items() if p == root]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack += [k for k, p in parent.items() if p == cur]
    return out


def envelope_region(vol: np.ndarray, L: int, parent: dict[int, int]) -> np.ndarray:
    """Solid region enclosed by tissue L's outer surface: L plus everything
    nested inside it, with internal cavities filled."""
    members = [L] + descendants(parent, L)
    return ndi.binary_fill_holes(np.isin(vol, members))


def _ball(r: int) -> np.ndarray:
    L = np.arange(-r, r + 1)
    Z, Y, X = np.meshgrid(L, L, L, indexing="ij")
    return (X * X + Y * Y + Z * Z) <= r * r


def seal_tunnels(region: np.ndarray, open_radius: int = 2, frac_thresh: float = 0.6) -> np.ndarray:
    """Reduce genus (close SEE-THROUGH "arch" tunnels, e.g. the skin bridge near
    the eye) while preserving bulk shape and volume. Morphological opening snaps
    the thin arch bridges (each removes a handle), then air that is mostly
    surrounded by body is filled back in (recovers the small volume the opening
    took, and fills shallow tunnel remnants). Keeps the largest component.

    Global closing does NOT work here — the tunnels are open on both ends, so
    closing either can't bridge them or creates new handles. Verified on the
    mouse: fill_holes body genus 4 → this → genus 0, dvol < 0.1%."""
    op = ndi.binary_fill_holes(ndi.binary_opening(region, _ball(open_radius)))
    lab, n = ndi.label(op)
    if n > 1:
        op = lab == (np.bincount(lab.ravel())[1:].argmax() + 1)
    bf = ndi.uniform_filter(op.astype(np.float32), size=2 * open_radius + 3)
    return ndi.binary_fill_holes(op | ((~op) & (bf > frac_thresh)))


# ── config / paths ─────────────────────────────────────────────────────────


def load_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def label_names(cfg: dict) -> dict[int, str]:
    info = cfg.get("info_txt")
    if info and os.path.exists(info):
        from parse_info_txt import parse_info_txt

        return parse_info_txt(info).label_names
    return {}


def spacing_zyx(cfg: dict) -> tuple[float, float, float]:
    info = cfg.get("info_txt")
    if info and os.path.exists(info):
        from parse_info_txt import parse_info_txt

        return tuple(parse_info_txt(info).spacing_zyx)
    return (1.0, 1.0, 1.0)


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="nested outer-envelope surfaces for surface-MC")
    ap.add_argument("--config", required=True, help="pipeline_config*.json")
    ap.add_argument(
        "--out", help="output dir (default: <output_dir>/vtk_export/conformal/surfaces_envelopes)"
    )
    ap.add_argument("--facet-size", type=float, default=0.12)
    ap.add_argument("--facet-distance", type=float, default=0.06)
    ap.add_argument("--cell-size", type=float, default=0.25)
    ap.add_argument("--taubin", type=int, default=30, help="surface_cleaner smoothing iterations")
    ap.add_argument("--min-faces", type=int, default=1000, help="drop components smaller than this")
    ap.add_argument(
        "--seal-tunnels",
        dest="seal_tunnels",
        action="store_true",
        default=True,
        help="close see-through arch tunnels (genus->0) on root tissues (default on)",
    )
    ap.add_argument(
        "--no-seal-tunnels",
        dest="seal_tunnels",
        action="store_false",
        help="keep anatomical tunnels/arches (skin genus stays >0)",
    )
    ap.add_argument(
        "--seal-open-radius",
        type=int,
        default=2,
        help="opening radius for tunnel sealing (larger = closes wider arches, more erosion)",
    )
    ap.add_argument(
        "--seal-labels",
        type=int,
        nargs="*",
        help="labels to topology-seal; default = root tissues (parent 0, e.g. skin)",
    )
    ap.add_argument(
        "--nest-margin",
        type=int,
        default=1,
        help="voxels an outer tissue must extend beyond its children so the "
        "smoothed surfaces never cross (0 disables; prevents skull poking "
        "through skin). Default 1.",
    )
    ap.add_argument(
        "--cgal-python", default=r"C:\Users\4elodoy Molovek\.conda\envs\cgal_env\python.exe"
    )
    ap.add_argument("--exe", help="mesh_and_remesh.exe (default from config remesh_exe)")
    ap.add_argument("--msys2-bin", help="MSYS2 ucrt64 bin for DLLs (default from config msys2_bin)")
    ap.add_argument(
        "--voxel-only", action="store_true", help="only write region .npy, skip meshing"
    )
    ap.add_argument("--keep-work", action="store_true", help="keep the per-tissue work dir")
    ap.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="parallel meshing jobs (tissues are independent). 0 = auto "
        "(~half the cores). Each CGAL mesh uses a few GB RAM, so cap "
        "for memory, not just cores.",
    )
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    out_dir = cfg["output_dir"]
    merged = os.path.join(out_dir, "02_merged.npy")
    if not os.path.exists(merged):
        print(f"[ERR] merged volume not found: {merged}", file=sys.stderr)
        return 2
    vol = np.load(merged)
    names = label_names(cfg)
    dz, dy, dx = spacing_zyx(cfg)
    labels = sorted(int(l) for l in np.unique(vol) if l != 0)

    ep = cfg.get("envelope_parents")
    if ep:
        parent = {int(k): int(v) for k, v in ep.items()}
        for L in labels:
            parent.setdefault(L, 0)
        src = "config envelope_parents"
    else:
        parent = derive_nesting(vol, labels)
        src = "AUTO-DERIVED (no envelope_parents in config)"
    print(f"Nesting ({src}) — label: parent (0=air):")
    for L in labels:
        print(
            f"  {L:>2} {names.get(L, f'label_{L}'):16} -> parent {parent[L]}"
            f"  descendants={descendants(parent, L)}"
        )
    if not ep:
        print(
            "[WARN] nesting was auto-derived and is FRAGILE for cropped/foraminated\n"
            "       volumes. VERIFY the tree above; if wrong, set 'envelope_parents'\n"
            '       in the config (e.g. {"2":1,"4":2,"5":2,"6":2,"7":2}).'
        )

    conformal = os.path.join(out_dir, "vtk_export", "conformal")
    out = args.out or os.path.join(conformal, "surfaces_envelopes")
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, "_work")
    os.makedirs(work, exist_ok=True)

    exe = args.exe or cfg.get("remesh_exe")
    msys_bin = args.msys2_bin or cfg.get("msys2_bin")
    env = dict(os.environ)
    if msys_bin and os.path.isdir(msys_bin):
        env["PATH"] = msys_bin + os.pathsep + env.get("PATH", "")

    from mc_mesh_check import export_surfaces

    npy2inr = os.path.join(HERE, "cgal_remesh", "npy2inr.py")

    seal_set = (
        set(args.seal_labels) if args.seal_labels else {L for L in labels if parent.get(L, 0) == 0}
    )
    if args.seal_tunnels:
        print(
            f"Tunnel sealing (genus->0) on labels {sorted(seal_set)} "
            f"(opening r={args.seal_open_radius}); others keep anatomical topology."
        )

    # 1) build every envelope region (+ optional tunnel sealing on roots)
    regions: dict[int, np.ndarray] = {}
    for L in labels:
        nm = names.get(L, f"label_{L}")
        region = envelope_region(vol, L, parent)
        if args.seal_tunnels and L in seal_set:
            before = int(region.sum())
            region = seal_tunnels(region, args.seal_open_radius)
            print(
                f"[{L}] {nm}: sealed tunnels, dvox={int(region.sum()) - before} "
                f"({100 * (int(region.sum()) - before) / max(before, 1):+.2f}%)"
            )
        regions[L] = region

    # 2) enforce nesting with a margin so the smoothed surfaces never cross:
    #    every parent must contain dilate(child, margin). Process children before
    #    parents (ascending size) so the growth propagates transitively
    #    (brain -> skull -> skin). This is what stops the skull/brain from poking
    #    through the skin after independent per-surface Taubin smoothing, and
    #    also repairs skin that tunnel-sealing eroded thin over the orbit.
    if args.nest_margin > 0:
        struct = _ball(args.nest_margin)
        for L in sorted(labels, key=lambda k: int(regions[k].sum())):
            p = parent.get(L, 0)
            if p:
                grown = ndi.binary_dilation(regions[L], struct)
                added = int((grown & ~regions[p]).sum())
                if added:
                    # fill_holes after the union: OR-ing a dilated child can seal a
                    # thin air gap into an internal cavity, which would otherwise
                    # mesh as a spurious 2nd (inner) surface component. Keep the
                    # largest solid piece as well.
                    merged = ndi.binary_fill_holes(regions[p] | grown)
                    lab, nlab = ndi.label(merged)
                    if nlab > 1:
                        merged = lab == (np.bincount(lab.ravel())[1:].argmax() + 1)
                    regions[p] = merged
                    print(
                        f"    nest: {names.get(p, p)} grew +{added} vox to contain "
                        f"{names.get(L, L)}+{args.nest_margin}"
                    )

    # 3) write region volumes (sequential, cheap)
    for L in labels:
        np.save(os.path.join(work, f"env_{L}.npy"), regions[L].astype(np.uint8))
        print(f"[{L}] {names.get(L, f'label_{L}')}: region voxels={int(regions[L].sum())}")

    saved = []
    if not args.voxel_only:
        # 4) mesh each region -> outer envelope surface. Tissues are INDEPENDENT
        #    (separate binary volumes), so mesh them in parallel; each
        #    mesh_and_remesh.exe is single-threaded, so this scales with cores
        #    (capped for RAM — each CGAL mesh needs a few GB).
        def mesh_one(L: int) -> str:
            nm = names.get(L, f"label_{L}")
            npy_p = os.path.join(work, f"env_{L}.npy")
            inr_p = os.path.join(work, f"env_{L}.inr")
            mesh_p = os.path.join(work, f"env_{L}.mesh")
            subprocess.run(
                [
                    args.cgal_python,
                    npy2inr,
                    "--npy",
                    npy_p,
                    "--spacing",
                    str(dz),
                    str(dy),
                    str(dx),
                    "--out",
                    inr_p,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    exe,
                    inr_p,
                    mesh_p,
                    "--facet-size",
                    str(args.facet_size),
                    "--facet-distance",
                    str(args.facet_distance),
                    "--cell-size",
                    str(args.cell_size),
                    "--no-remesh",
                    "--max-tets",
                    "30000000",
                ],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )
            tmp = os.path.join(work, f"tmp_{L}")
            os.makedirs(tmp, exist_ok=True)
            export_surfaces(mesh_p, tmp, {1: nm})
            dst = os.path.join(out, f"surface_{L:02d}_{nm}.vtk")
            shutil.copy(os.path.join(tmp, f"surface_01_{nm}.vtk"), dst)
            return dst

        jobs = args.jobs or max(1, min(len(labels), (os.cpu_count() or 4) // 2))
        print(f"\nMeshing {len(labels)} tissues, {jobs} parallel job(s)...")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(mesh_one, L): L for L in labels}
            for fut in as_completed(futs):
                L = futs[fut]
                try:
                    dst = fut.result()
                    saved.append(dst)
                    print(f"  [{names.get(L, L)}] meshed -> {os.path.basename(dst)}")
                except subprocess.CalledProcessError as e:
                    print(f"  [{names.get(L, L)}] FAILED: {e.stderr or e}", file=sys.stderr)
                    raise

    if not args.voxel_only and saved:
        print("\nCleaning surfaces (Taubin + outward normals)...")
        subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "surface_cleaner.py"),
                "--dir",
                out,
                "--taubin",
                str(args.taubin),
                "--min-faces",
                str(args.min_faces),
                "--jobs",
                str(args.jobs),
            ],
            check=True,
        )
        # optical-property template (same labels; names carry them)
        import csv

        with open(
            os.path.join(out, "optical_properties.csv"), "w", newline="", encoding="utf-8"
        ) as f:
            w = csv.writer(f)
            w.writerow(["label", "name", "mua_1/mm", "mus_1/mm", "g", "n"])
            for L in labels:
                w.writerow([L, names.get(L, f"label_{L}"), "", "", "", ""])
        print(f"\nDone. {len(saved)} envelope surfaces + optical_properties.csv in:\n  {out}")

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
