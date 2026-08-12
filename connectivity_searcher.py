# -*- coding: utf-8 -*-
"""
Поиск связанных областей в 3D-массиве.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

try:
    import matplotlib.pyplot as plt

    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
import numpy as np
from scipy import ndimage
from scipy.ndimage import label
from tqdm import tqdm

# Unicode-символы в логе (→, ─) не должны ронять StreamHandler на не-UTF-8
# кодовой странице Windows (cp1251/cp1252) — иначе UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

LABEL_NAMES: Dict[int, str] = {
    0: "Background",
    1: "CSF",
    2: "Gray Matter",
    3: "White Matter",
    4: "Fat",
    5: "Muscle",
    6: "Muscle/Skin",
    7: "Skull",
    8: "Vessels",
    9: "Around Fat",
    10: "Dura Matter",
    11: "Bone Marrow",
}

# old_label → new_label после перекодировки
_RECODE_MAP: Dict[int, int] = {
    1: 3,
    2: 3,
    8: 3,  # CSF, GM, Vessels → White Matter
    4: 6,
    5: 6,
    7: 6,
    9: 6,
    10: 6,
    11: 6,  # жир/мышцы/кость → Skin
}


def recode_labels(volume: np.ndarray) -> np.ndarray:
    """Перекодирует метки in-place согласно _RECODE_MAP."""
    log.info("Перекодировка меток...")
    for old, new in _RECODE_MAP.items():
        volume[volume == old] = new
    remaining = np.unique(volume).tolist()
    log.info("Метки после перекодировки: %s", remaining)
    return volume


def relabel_existing_labels(
    volume: np.ndarray,
    connectivity: int = 1,
) -> Tuple[np.ndarray, int]:
    """
    Находит связные компоненты внутри каждой метки и присваивает им
    уникальные целочисленные идентификаторы.

    Args:
        volume: 3-D массив с целочисленными метками тканей.
        connectivity: 1 = 6-связность, 2 = 18-связность, 3 = 26-связность.

    Returns:
        Кортеж (relabeled, n_total), где n_total — общее число компонент.
    """
    if connectivity not in (1, 2, 3):
        raise ValueError(f"connectivity должно быть 1, 2 или 3, получено {connectivity}")

    structure = ndimage.generate_binary_structure(3, connectivity)
    log.info("Поиск связных компонент (connectivity=%d)...", connectivity)

    relabeled = np.zeros_like(volume, dtype=np.int32)
    next_label = 1

    unique_labels: List[int] = [int(l) for l in np.unique(volume) if l != 0]

    for lbl in tqdm(unique_labels, desc="Обработка меток"):
        mask = volume == lbl
        labeled_mask, n = label(mask, structure=structure)
        if n == 0:
            continue
        labeled_mask[labeled_mask > 0] += next_label - 1
        relabeled += labeled_mask
        next_label += n

    total = next_label - 1
    log.info("Найдено %d связных компонент.", total)
    return relabeled, total


def visualize_slices(
    before: np.ndarray,
    after: np.ndarray,
    num_slices: int = 5,
    save_dir: Optional[str] = None,
) -> None:
    if not _HAS_MPL:
        log.info("matplotlib недоступен, визуализация пропущена")
        return
    depth = before.shape[0]
    step = max(1, depth // num_slices)
    slices = list(range(0, depth, step))[:num_slices]

    fig, axes = plt.subplots(2, num_slices, figsize=(4 * num_slices, 8))
    for i, s in enumerate(slices):
        axes[0, i].imshow(before[s], cmap="tab20")
        axes[0, i].set_title(f"До (срез {s})")
        axes[0, i].axis("off")

        axes[1, i].imshow(after[s], cmap="tab20")
        axes[1, i].set_title(f"После (срез {s})")
        axes[1, i].axis("off")

    plt.tight_layout()

    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        out = os.path.join(save_dir, "01_connectivity_slices.png")
        fig.savefig(out, dpi=100, bbox_inches="tight")
        log.info("График сохранён: %s", out)

    backend = plt.get_backend().lower()
    if "agg" not in backend:
        plt.show(block=False)
        plt.pause(2)

    plt.close(fig)


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument("--config", default="pipeline_config.json")
    _cfg_path = _parser.parse_args().config

    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # ── Читаем INFO.txt (универсальный формат) ────────────────────────
    info_txt_path: Optional[str] = cfg.get("info_txt")
    if info_txt_path and os.path.exists(info_txt_path):
        import sys as _sys

        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from parse_info_txt import parse_info_txt as _parse_info

        _info = _parse_info(info_txt_path)
        log.info("INFO.txt: %s", _info)
        z_start_mm_info: Optional[float] = _info.z_start_mm
        z_step_mm_info: float = _info.dz
        y_step_mm_info: float = _info.dy
        label_merges_info: Dict[int, int] = _info.label_merges
    else:
        # Обратная совместимость: берём из полей конфига
        z_start_mm_info = cfg.get("z_start_mm")
        z_step_mm_info = abs(float(cfg.get("z_step_mm", 1.0)))
        y_step_mm_info = abs(float(cfg.get("y_step_mm", z_step_mm_info)))
        label_merges_info = {int(k): int(v) for k, v in cfg.get("label_merges", {}).items()}

    input_path: str = cfg["input_volume"]
    os.makedirs(cfg["output_dir"], exist_ok=True)
    output_path: str = os.path.join(cfg["output_dir"], "01_labeled.npy")

    log.info("Загрузка: %s", input_path)
    volume: np.ndarray = np.load(input_path)
    log.info("Размерность: %s, dtype=%s", volume.shape, volume.dtype)

    # ── Опциональная обрезка по Z ─────────────────────────────────────
    z_cut_mm_cfg = cfg.get("z_cut_mm")
    if z_cut_mm_cfg is not None and z_start_mm_info is not None:
        z_cut_idx: int = int((float(z_cut_mm_cfg) - z_start_mm_info) / z_step_mm_info)
        log.info("Обрезка по Z: уровень=%.1f мм → индекс %d", z_cut_mm_cfg, z_cut_idx)
        volume = volume[z_cut_idx:, :, :]
        log.info("Новая размерность: %s", volume.shape)
    elif z_cut_mm_cfg is not None:
        log.warning(
            "z_cut_mm задан (%.1f), но z_start_mm отсутствует → пропускаем обрезку", z_cut_mm_cfg
        )

    # ── Опциональная обрезка по Y (например, для головы мыши) ────────
    y_end = cfg.get("y_end_idx")
    if y_end is not None:
        log.info("Обрезка по Y: оставляем [:, :%d, :] (%.1f мм)", y_end, y_end * y_step_mm_info)
        volume = volume[:, : int(y_end), :]
        log.info("Новая размерность: %s", volume.shape)

    # ── Слияния меток из INFO.txt ─────────────────────────────────────
    if label_merges_info:
        for src, dst in label_merges_info.items():
            n_src = int(np.sum(volume == src))
            volume[volume == src] = dst
            log.info("  label_merge: %d → %d (%d вокселей)", src, dst, n_src)

    labeled, n = relabel_existing_labels(volume, connectivity=3)
    log.info("Всего компонент: %d", n)

    plots_dir = os.path.join(cfg["output_dir"], "plots")
    visualize_slices(volume, labeled, num_slices=5, save_dir=plots_dir)

    np.save(output_path, labeled)
    log.info("Результат сохранён: %s", output_path)
