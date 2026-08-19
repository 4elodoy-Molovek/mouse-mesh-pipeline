# -*- coding: utf-8 -*-
"""
Split a solid brain into grey (outer shell) + white (inner core) by erosion.

Optional preprocessing stage for atlases that segment the brain only as one
solid body (or as anatomical sub-parts that overlap when nested, e.g. Digimouse),
so they have no real grey/white matter. We synthesise it: take the whole brain,
erode it by the grey-matter thickness to get the white-matter core, and let the
remaining shell be grey matter. The result is a strictly nested
    skin > skull > grey > white
model with no hole and no sibling overlap -- valid for surface Monte-Carlo,
where the layer across a surface must be unambiguous.

The boundary is geometric, not histological: for light transport the point is to
carry distinct optical properties per shell, which this delivers, with a tunable
grey thickness. Runs on 02_merged.npy after the standard preprocessing and before
build_envelopes; it is idempotent (re-runs rebuild the whole brain from the
current grey+white before splitting again).

Config keys (pipeline_config*.json):
    grey_white_split   bool   enable the stage (the GUI checkbox writes this)
    gw_brain_labels    [int]  labels in 02_merged that together form the brain
    gw_grey_mm         float  grey shell thickness in mm (default 0.6)
    gw_white_label     int    label assigned to the white-matter core (default 3)
    gw_grey_label      int    label assigned to the grey-matter shell (default 4)
Pair with envelope_parents nesting grey>white and label_names_extra naming both,
so build_envelopes / surface_metrics / render_surfaces label them correctly.

Usage:
    python grey_white_split.py --config pipeline_config_mouse_gw.json
    python grey_white_split.py --config ... --grey-mm 0.8
    python grey_white_split.py --npy 02_merged.npy --brain-labels 4 --grey-mm 0.6
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from scipy import ndimage as ndi

# Keep Cyrillic/Unicode console output from crashing on a non-UTF-8 Windows
# code page (cp1251/cp1252) -- same guard as the other pipeline scripts.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _min_spacing(cfg: dict) -> float:
    info = cfg.get("info_txt")
    if info and os.path.exists(info):
        from parse_info_txt import parse_info_txt

        return float(min(parse_info_txt(info).spacing_zyx))
    return 1.0


def split_grey_white(
    vol: np.ndarray,
    brain_labels: list[int],
    grey_vox: int,
    white_label: int,
    grey_label: int,
) -> np.ndarray:
    """Return a copy of `vol` with the brain replaced by grey shell + white core.

    The brain is rebuilt as union(brain_labels + {white_label, grey_label}) so a
    re-run reconstructs the whole brain before eroding again (idempotent). Holes
    are filled first so the core is solid; erosion by `grey_vox` voxels carves the
    white core, the remaining shell is grey.
    """
    wanted = set(int(l) for l in brain_labels) | {int(white_label), int(grey_label)}
    brain = ndi.binary_fill_holes(np.isin(vol, list(wanted)))
    if not brain.any():
        return vol.copy()
    white = ndi.binary_erosion(brain, iterations=max(1, int(grey_vox)))
    grey = brain & ~white
    out = vol.copy()
    out[grey] = grey_label
    out[white] = white_label
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Split a solid brain into grey shell + white core")
    ap.add_argument("--config", help="pipeline_config*.json (splits 02_merged.npy in place)")
    ap.add_argument("--npy", help="explicit labeled .npy (overrides --config)")
    ap.add_argument("--out", help="output path (default: overwrite input)")
    ap.add_argument("--grey-mm", type=float, default=None, help="grey thickness in mm")
    ap.add_argument(
        "--brain-labels",
        type=int,
        nargs="+",
        default=None,
        help="labels forming the brain (default: config gw_brain_labels)",
    )
    ap.add_argument("--white-label", type=int, default=None)
    ap.add_argument("--grey-label", type=int, default=None)
    args = ap.parse_args()

    cfg = {}
    if args.npy:
        in_path = args.npy
    else:
        if not args.config:
            ap.error("need --config or --npy")
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        in_path = os.path.join(cfg["output_dir"], "02_merged.npy")
    out_path = args.out or in_path

    brain_labels = args.brain_labels or [int(l) for l in cfg.get("gw_brain_labels", [])]
    if not brain_labels:
        print("[grey_white_split] no gw_brain_labels -> nothing to split", file=sys.stderr)
        return 0
    white_label = (
        args.white_label if args.white_label is not None else int(cfg.get("gw_white_label", 3))
    )
    grey_label = (
        args.grey_label if args.grey_label is not None else int(cfg.get("gw_grey_label", 4))
    )
    grey_mm = args.grey_mm if args.grey_mm is not None else float(cfg.get("gw_grey_mm", 0.6))
    spacing = _min_spacing(cfg)
    grey_vox = max(1, round(grey_mm / spacing))

    print(f"Загрузка: {in_path}")
    vol = np.load(in_path)
    print(
        f"  brain labels={brain_labels}  grey={grey_mm} мм = {grey_vox} воксель "
        f"(spacing {spacing:g} мм)  white->{white_label}  grey->{grey_label}"
    )
    out = split_grey_white(vol, brain_labels, grey_vox, white_label, grey_label)

    gv = int(np.count_nonzero(out == grey_label))
    wv = int(np.count_nonzero(out == white_label))
    print(f"Результат: grey={gv} вокс, white={wv} вокс (мозг {gv + wv})")
    np.save(out_path, out)
    print(f"Сохранено: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
