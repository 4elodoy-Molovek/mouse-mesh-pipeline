#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert an Analyze 7.5 .hdr/.img atlas file to .npy.

Reads volume dimensions from INFO.txt (Matrix size: nX x nY x nZ).
Reads data type from the companion .hdr file (offset 70, Analyze 7.5 spec).
Reshapes raw binary data to (nZ, nY, nX) — standard numpy axis order.

Usage:
    python img2npy.py <img_file> <info_txt> <output_npy>

Example:
    python img2npy.py G:/nauchka/utilities/CRI_Data/mouse/atlas/atlas_380x992x208.img \
                      G:/nauchka/utilities/CRI_Data/mouse/atlas/INFO.txt \
                      G:/nauchka/utilities/CRI_Data/mouse/atlas/atlas.npy
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

import numpy as np

# Analyze 7.5 datatype codes → numpy dtypes
_DTYPE_MAP: dict[int, type] = {
    2: np.uint8,
    4: np.int16,
    8: np.int32,
    16: np.float32,
    64: np.float64,
}


def _read_hdr_dtype(hdr_path: str) -> np.dtype:
    """Return numpy dtype from Analyze 7.5 .hdr (datatype at byte offset 70)."""
    with open(hdr_path, "rb") as f:
        data = f.read(348)
    if len(data) < 72:
        print(f"[WARN] .hdr file too short ({len(data)} bytes) — assuming uint8")
        return np.dtype(np.uint8)
    dt_code = struct.unpack_from("h", data, 70)[0]
    dtype = _DTYPE_MAP.get(dt_code)
    if dtype is None:
        print(f"[WARN] Unknown Analyze datatype code {dt_code} — assuming uint8")
        return np.dtype(np.uint8)
    return np.dtype(dtype)


def convert(img_path: str, info_txt_path: str, output_npy: str) -> None:
    # Import here so the module can be imported without parse_info_txt on sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from parse_info_txt import parse_info_txt

    info = parse_info_txt(info_txt_path)
    if info.matrix_size is None:
        raise ValueError(f"INFO.txt has no 'Matrix size' line: {info_txt_path}")

    nX, nY, nZ = info.matrix_size

    # Detect dtype from .hdr if available
    hdr_path = os.path.splitext(img_path)[0] + ".hdr"
    if os.path.exists(hdr_path):
        dtype = _read_hdr_dtype(hdr_path)
        print(f"dtype from .hdr: {dtype}")
    else:
        dtype = np.dtype(np.uint8)
        print(f"[WARN] {hdr_path} not found — assuming uint8")

    print(f"Reading: {img_path}")
    print(f"  expected shape: (nZ={nZ}, nY={nY}, nX={nX})  dtype={dtype}")

    data = np.fromfile(img_path, dtype=dtype)
    expected = nZ * nY * nX
    if data.size != expected:
        raise ValueError(
            f"File has {data.size} elements, expected {expected} "
            f"({nZ}×{nY}×{nX}).  Check Matrix size in INFO.txt or dtype."
        )

    volume = data.reshape(nZ, nY, nX)
    os.makedirs(os.path.dirname(os.path.abspath(output_npy)), exist_ok=True)
    np.save(output_npy, volume)

    labels = sorted(int(v) for v in np.unique(volume))
    print(f"Saved: {output_npy}")
    print(f"  shape={volume.shape}  dtype={volume.dtype}")
    print(f"  voxel size: dx={info.dx}mm  dy={info.dy}mm  dz={info.dz}mm")
    print(f"  labels ({len(labels)}): {labels}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert Analyze 7.5 .hdr/.img atlas to .npy")
    parser.add_argument("img", help="Path to .img file")
    parser.add_argument("info_txt", help="Path to INFO.txt with 'Matrix size'")
    parser.add_argument("output", help="Output .npy path")
    args = parser.parse_args()
    convert(args.img, args.info_txt, args.output)
