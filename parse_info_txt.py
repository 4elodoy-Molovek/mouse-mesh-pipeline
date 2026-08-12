# -*- coding: utf-8 -*-
"""
Universal INFO.txt parser for atlas datasets.

Supported format (all lines optional except labels):

    Matrix size: 380 x 992 x 208   # nX x nY x nZ
    Voxel size: 0.1mm x 0.1mm x 0.1mm  # dx x dy x dz  (same axis order)
    Z start: -72.25mm               # physical coord of slice index 0

    0 --> background
    1 --> skin
    4,9 --> Fat          # comma-separated → merged into one class (canonical = min label)
    4+5+6 --> whole brain  # plus-separated → SKIPPED (informational compound)
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple


class AtlasInfo:
    def __init__(
        self,
        matrix_size: Optional[Tuple[int, int, int]],
        voxel_size: Optional[Tuple[float, float, float]],
        z_start_mm: Optional[float],
        label_names: Dict[int, str],
        label_merges: Dict[int, int],
        background_labels: Set[int],
    ) -> None:
        self.matrix_size = matrix_size  # (nX, nY, nZ) — matches Matrix size line
        self._voxel_size = voxel_size  # (dx, dy, dz) mm — matches Voxel size line
        self.z_start_mm = z_start_mm  # mm coord of first Z slice, or None
        self._label_names = label_names  # canonical_label → class_name
        self._label_merges = label_merges  # src_label → canonical_label (src != canonical)
        self._background_labels = background_labels

    # ── spacing accessors ──────────────────────────────────────────────

    @property
    def dx(self) -> float:
        return self._voxel_size[0] if self._voxel_size else 1.0

    @property
    def dy(self) -> float:
        return self._voxel_size[1] if self._voxel_size else 1.0

    @property
    def dz(self) -> float:
        return self._voxel_size[2] if self._voxel_size else 1.0

    @property
    def spacing_zyx(self) -> Tuple[float, float, float]:
        """(dz, dy, dx) — for numpy array shape (nZ, nY, nX); use with pygalmesh."""
        return (self.dz, self.dy, self.dx)

    @property
    def spacing_xyz(self) -> Tuple[float, float, float]:
        """(dx, dy, dz) — VTK SetSpacing order."""
        return (self.dx, self.dy, self.dz)

    # ── label accessors ────────────────────────────────────────────────

    @property
    def label_names(self) -> Dict[int, str]:
        """canonical_label → class_name, background excluded."""
        return dict(self._label_names)

    @property
    def label_merges(self) -> Dict[int, int]:
        """src_label → canonical_label. Includes {N: 0} for non-zero background labels."""
        merges = dict(self._label_merges)
        for lbl in self._background_labels:
            if lbl != 0:
                merges[lbl] = 0
        return merges

    @property
    def tissue_labels(self) -> List[int]:
        """Sorted canonical non-background label ids."""
        return sorted(self._label_names.keys())

    def __repr__(self) -> str:
        vs = f"({self.dx},{self.dy},{self.dz})mm" if self._voxel_size else "unknown"
        return (
            f"AtlasInfo(matrix={self.matrix_size}, spacing={vs}, "
            f"z_start={self.z_start_mm}, "
            f"labels={len(self._label_names)}, merges={len(self._label_merges)})"
        )


def parse_info_txt(path: str) -> AtlasInfo:
    """Parse a universal INFO.txt and return an AtlasInfo instance."""
    matrix_size: Optional[Tuple[int, int, int]] = None
    voxel_size: Optional[Tuple[float, float, float]] = None
    z_start_mm: Optional[float] = None
    label_names: Dict[int, str] = {}
    label_merges: Dict[int, int] = {}
    background_labels: Set[int] = set()

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            # Срезаем комментарии: как строки целиком (# ...), так и inline (name  # note)
            line = line.split("#", 1)[0].strip()
            if not line:
                continue

            # Matrix size: 380 x 992 x 208
            m = re.match(
                r"[Mm]atrix\s+[Ss]ize\s*[:：]\s*(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+)",
                line,
            )
            if m:
                matrix_size = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                continue

            # Voxel size: 0.1mm x 0.1mm x 0.1mm
            m = re.match(
                r"[Vv]oxel\s+[Ss]ize\s*[:：]\s*([0-9.]+)\s*mm\s*[x×]\s*([0-9.]+)\s*mm\s*[x×]\s*([0-9.]+)\s*mm",
                line,
            )
            if m:
                voxel_size = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                continue

            # Z start: -72.25mm
            m = re.match(r"[Zz]\s+[Ss]tart\s*[:：]\s*([+-]?[0-9.]+)\s*mm?", line)
            if m:
                z_start_mm = float(m.group(1))
                continue

            # Skip compound informational lines that use '+' (e.g. "4+5+6 --> whole brain")
            lhs = line.split("-->")[0] if "-->" in line else ""
            if lhs and "+" in lhs and "," not in lhs:
                continue

            # Label mapping: "0 --> background"  or  "4,9 --> Fat"
            m = re.match(r"([\d,\s]+)\s*-->\s*(.+)", line)
            if m:
                src_str = m.group(1).strip()
                name = m.group(2).strip()
                src_labels = [int(x.strip()) for x in src_str.split(",") if x.strip().isdigit()]
                if not src_labels:
                    continue

                canonical = min(src_labels)
                is_bg = (0 in src_labels) or ("background" in name.lower())

                if is_bg:
                    background_labels.update(src_labels)
                else:
                    # A name might appear on multiple lines (e.g., extending a group).
                    # Use the smaller canonical label if the name already exists.
                    existing = next((k for k, v in label_names.items() if v == name), None)
                    if existing is not None and existing < canonical:
                        canonical = existing
                    label_names[canonical] = name
                    for src in src_labels:
                        if src != canonical:
                            label_merges[src] = canonical

    return AtlasInfo(
        matrix_size=matrix_size,
        voxel_size=voxel_size,
        z_start_mm=z_start_mm,
        label_names=label_names,
        label_merges=label_merges,
        background_labels=background_labels,
    )


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "INFO.txt"
    info = parse_info_txt(path)
    print(info)
    print(f"  dx={info.dx}  dy={info.dy}  dz={info.dz}")
    print(f"  z_start_mm={info.z_start_mm}")
    print(f"  tissue labels: {info.tissue_labels}")
    print(f"  label_names: {info.label_names}")
    print(f"  label_merges: {info.label_merges}")
