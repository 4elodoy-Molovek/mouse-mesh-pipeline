# -*- coding: utf-8 -*-
"""
Convert a labeled .npy volume to an uncompressed INRIMAGE (.inr) that the
CGAL C++ tool (mesh_and_remesh) reads via CGAL::Image_3.

Pure NumPy — the .inr writer here is byte-for-byte identical to pygalmesh's, so
this runs in the MAIN env (no pygalmesh / cgal_env needed). Spacing is taken from
INFO.txt (dz, dy, dx) to match the (nZ, nY, nX) array order, so the .inr voxel
sizes line up with the rest of the pipeline.

Usage:
    python npy2inr.py --config pipeline_config_mouse.json [--out path.inr]
    python npy2inr.py --npy 02_merged.npy --spacing 0.1 0.1 0.1 --out out.inr
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np


def save_inr(vol: np.ndarray, spacing, path: str) -> None:
    """Write a labeled 3-D uint8/uint16 array as an uncompressed INRIMAGE (.inr),
    the format CGAL's Image_3 / Mesh_3 reads. Pure NumPy — byte-for-byte identical
    to pygalmesh.save_inr (verified), so no pygalmesh / cgal_env is needed.

    The 256-byte ASCII header maps XDIM<-shape[0], YDIM<-shape[1], ZDIM<-shape[2]
    with voxel sizes VX,VY,VZ = spacing; voxels are stored X-fastest (Fortran
    order of the (nZ,nY,nX) array), matching the rest of the pipeline's axis
    convention (spacing = (dz,dy,dx))."""
    vol = np.ascontiguousarray(vol)
    if vol.ndim != 3:
        raise ValueError(f"expected a 3-D array, got shape {vol.shape}")
    if vol.dtype == np.uint8:
        pixsize = 8
    elif vol.dtype == np.uint16:
        pixsize = 16
    else:
        raise ValueError(f"INR needs uint8/uint16, got {vol.dtype}")
    h = "#INRIMAGE-4#{\n"
    h += f"XDIM={vol.shape[0]}\nYDIM={vol.shape[1]}\nZDIM={vol.shape[2]}\nVDIM=1\n"
    h += f"TYPE=unsigned fixed\nPIXSIZE={pixsize} bits\nCPU=decm\n"
    h += f"VX={spacing[0]:.6f}\nVY={spacing[1]:.6f}\nVZ={spacing[2]:.6f}\n"
    h += "\n" * (256 - len(h) - 4) + "##}\n"
    assert len(h) == 256, len(h)
    with open(path, "wb") as f:
        f.write(h.encode("ascii"))
        f.write(vol.tobytes(order="F"))


def main() -> int:
    ap = argparse.ArgumentParser(description="labeled .npy -> INRIMAGE .inr")
    ap.add_argument("--config", help="pipeline_config*.json (reads 02_merged.npy + INFO spacing)")
    ap.add_argument("--npy", help="explicit labeled .npy (overrides --config)")
    ap.add_argument(
        "--spacing",
        nargs=3,
        type=float,
        metavar=("DZ", "DY", "DX"),
        help="voxel size for the (nZ,nY,nX) array; default from INFO/1.0",
    )
    ap.add_argument(
        "--out", help="output .inr path (default: <output_dir>/vtk_export/conformal/volume.inr)"
    )
    args = ap.parse_args()

    spacing = tuple(args.spacing) if args.spacing else None
    out = args.out

    if args.npy:
        npy_path = args.npy
        if spacing is None:
            spacing = (1.0, 1.0, 1.0)
    else:
        if not args.config:
            ap.error("need --config or --npy")
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        npy_path = os.path.join(cfg["output_dir"], "02_merged.npy")
        if spacing is None:
            info_txt = cfg.get("info_txt")
            if info_txt and os.path.exists(info_txt):
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from parse_info_txt import parse_info_txt

                spacing = parse_info_txt(info_txt).spacing_zyx  # (dz, dy, dx)
            else:
                spacing = (1.0, 1.0, 1.0)
        if out is None:
            out = os.path.join(cfg["output_dir"], "vtk_export", "conformal", "volume.inr")

    vol = np.load(npy_path)
    # CGAL labeled-image domain wants an unsigned integer image; the label
    # values become the subdomain ids directly. uint8 if it fits, else uint16.
    max_label = int(vol.max())
    dtype = np.uint8 if max_label < 256 else np.uint16
    vol = np.ascontiguousarray(vol.astype(dtype))

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    save_inr(vol, [float(s) for s in spacing], out)

    labels = sorted(int(l) for l in np.unique(vol))
    print(f"wrote {out}")
    print(f"  shape={vol.shape} (nZ,nY,nX)  dtype={vol.dtype}  spacing(dz,dy,dx)={tuple(spacing)}")
    print(f"  labels={labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
