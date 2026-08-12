#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert a raw-bytes (.rawb) volume to .npy.

Format: headerless uint8 binary, shape read from INFO.txt (Matrix size: nX x nY x nZ).

Usage:
    python rawb2npy.py <rawb_file> <info_txt> <output_npy>
"""

import argparse
import os
import sys

import numpy as np


def convert(rawb_path: str, info_txt_path: str, output_npy: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from parse_info_txt import parse_info_txt

    info = parse_info_txt(info_txt_path)
    if info.matrix_size is None:
        raise ValueError(f"INFO.txt has no 'Matrix size' line: {info_txt_path}")

    nX, nY, nZ = info.matrix_size
    expected = nZ * nY * nX

    print(f"Reading: {rawb_path}")
    print(f"  expected shape: (nZ={nZ}, nY={nY}, nX={nX})  dtype=uint8")

    with open(rawb_path, "rb") as f:
        data = np.frombuffer(f.read(), dtype=np.uint8)

    if data.size != expected:
        raise ValueError(
            f"File has {data.size} bytes, expected {expected} ({nZ}×{nY}×{nX})."
            " Check Matrix size in INFO.txt."
        )

    volume = data.reshape(nZ, nY, nX)
    os.makedirs(os.path.dirname(os.path.abspath(output_npy)), exist_ok=True)
    np.save(output_npy, volume)

    labels = sorted(int(v) for v in np.unique(volume))
    print(f"Saved: {output_npy}")
    print(f"  shape={volume.shape}  dtype={volume.dtype}")
    print(f"  labels ({len(labels)}): {labels}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert .rawb volume to .npy")
    parser.add_argument("rawb", help="Path to .rawb file")
    parser.add_argument("info_txt", help="Path to INFO.txt with 'Matrix size'")
    parser.add_argument("output", help="Output .npy path")
    args = parser.parse_args()
    convert(args.rawb, args.info_txt, args.output)
