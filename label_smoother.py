# -*- coding: utf-8 -*-
"""
Multi-label voxel smoothing (preprocessing before mesh generation).

Why: thin, jagged, self-touching tissue boundaries in the voxel volume force
the mesher to emit non-manifold surface pinches — the "craters"/spikes on the
skin and skull. Smoothing the *label field* before meshing melts those thin
self-contacts, so Mesh_3 / pygalmesh produce cleaner, more nearly-manifold
surfaces, while the shapes stay in place.

Method (label-field Gaussian argmax): for every label (background included) take
its 0/1 indicator, blur it with a Gaussian, then assign each voxel to the label
whose blurred indicator is largest. This rounds boundaries and removes thin
protrusions/bridges without moving the bulk of any region. Background takes part
in the vote, so thin skin spikes erode into background and thin background gaps
fill in — exactly what removes the pinch points.

Idempotent: the un-smoothed volume is kept as `<stem>.orig.npy`; each run smooths
from that original, so re-running with a new sigma does not compound.

Usage:
    python label_smoother.py --config pipeline_config_mouse.json           # sigma from config
    python label_smoother.py --config pipeline_config_mouse.json --sigma 1.5
    python label_smoother.py --npy 02_merged.npy --sigma 1.0 --out smoothed.npy
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Optional

import numpy as np
from scipy.ndimage import gaussian_filter

# Keep Cyrillic/Unicode console output from crashing on a non-UTF-8 Windows
# code page (cp1251/cp1252) — same guard as the other pipeline scripts.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def smooth_labels(volume: np.ndarray, sigma: float, protect: Optional[set] = None) -> np.ndarray:
    """Return a label-smoothed copy of `volume` (Gaussian argmax over labels).

    sigma is in voxels (isotropic). `protect` is an optional set of label ids
    that must not vanish: if smoothing would erase a protected label entirely,
    its original voxels are restored.
    """
    labels = [int(l) for l in np.unique(volume)]
    best_val = np.full(volume.shape, -1.0, dtype=np.float32)
    best_lbl = np.zeros(volume.shape, dtype=volume.dtype)

    for lbl in labels:
        ind = (volume == lbl).astype(np.float32)
        blurred = gaussian_filter(ind, sigma=sigma, mode="nearest")
        take = blurred > best_val
        best_lbl[take] = lbl
        best_val[take] = blurred[take]
        del ind, blurred

    if protect:
        for lbl in protect:
            if lbl == 0:
                continue
            if not np.any(best_lbl == lbl) and np.any(volume == lbl):
                # smoothing wiped this tissue out — put its original voxels back
                best_lbl[volume == lbl] = lbl
    return best_lbl


def _report(before: np.ndarray, after: np.ndarray) -> None:
    lb, cb = np.unique(before, return_counts=True)
    la, ca = np.unique(after, return_counts=True)
    cbd = {int(l): int(c) for l, c in zip(lb, cb)}
    cad = {int(l): int(c) for l, c in zip(la, ca)}
    changed = int(np.count_nonzero(before != after))
    print(f"Изменено вокселей: {changed} ({100.0 * changed / before.size:.2f}%)")
    print(f"{'label':>6} {'before':>12} {'after':>12} {'delta%':>8}")
    for lbl in sorted(set(cbd) | set(cad)):
        b = cbd.get(lbl, 0)
        a = cad.get(lbl, 0)
        d = (100.0 * (a - b) / b) if b else float("nan")
        flag = "  <-- ИСЧЕЗЛА" if b > 0 and a == 0 else ""
        print(f"{lbl:>6} {b:>12} {a:>12} {d:>7.1f}%{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-label voxel smoothing")
    ap.add_argument("--config", help="pipeline_config*.json (smooths 02_merged.npy in place)")
    ap.add_argument("--npy", help="explicit labeled .npy (overrides --config)")
    ap.add_argument("--out", help="output path (default: overwrite input, keep <stem>.orig.npy)")
    ap.add_argument(
        "--sigma",
        type=float,
        default=None,
        help="Gaussian sigma in voxels (default: config label_smooth_sigma or 1.0)",
    )
    ap.add_argument(
        "--no-protect",
        action="store_true",
        help="allow small tissues to vanish (default: protect them)",
    )
    args = ap.parse_args()

    sigma = args.sigma
    if args.npy:
        in_path = args.npy
        out_path = args.out or in_path
    else:
        if not args.config:
            ap.error("need --config or --npy")
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        in_path = os.path.join(cfg["output_dir"], "02_merged.npy")
        out_path = args.out or in_path
        if sigma is None:
            sigma = float(cfg.get("label_smooth_sigma", 1.0))
    if sigma is None:
        sigma = 1.0

    # Idempotency: always smooth from the pristine original.
    stem, ext = os.path.splitext(in_path)
    orig_path = stem + ".orig" + ext
    if out_path == in_path:
        if os.path.exists(orig_path):
            source = orig_path
        else:
            shutil.copy2(in_path, orig_path)
            source = orig_path
        print(f"Источник (оригинал): {source}")
    else:
        source = in_path

    print(f"Загрузка: {source}")
    volume = np.load(source)
    print(f"  shape={volume.shape} dtype={volume.dtype}  sigma={sigma} воксель")

    protect = None if args.no_protect else set(int(l) for l in np.unique(volume) if l != 0)
    smoothed = smooth_labels(volume, sigma=sigma, protect=protect)
    _report(volume, smoothed)

    np.save(out_path, smoothed)
    print(f"Результат сохранён: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
