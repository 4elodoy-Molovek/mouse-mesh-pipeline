# -*- coding: utf-8 -*-
import numpy as np

try:
    import matplotlib.pyplot as plt
    import matplotlib as _matplotlib

    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
from scipy.ndimage import distance_transform_edt
from tqdm import tqdm
import csv
import json
import os

LABEL_NAMES = {
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


def merge_small_regions(labeled_volume, min_voxels=10000):
    labeled = labeled_volume.copy()
    labels = np.unique(labeled)
    labels = labels[labels != 0]

    counts = np.bincount(labeled.ravel())
    small_labels = [lbl for lbl in labels if counts[lbl] < min_voxels]
    large_labels = [lbl for lbl in labels if counts[lbl] >= min_voxels]

    print(f"Найдено {len(small_labels)} маленьких областей (< {min_voxels} вокселей)")
    print(f"Найдено {len(large_labels)} больших областей")

    if not small_labels or not large_labels:
        print("Нечего объединять — возвращаю исходный объём.")
        return labeled

    large_mask = np.isin(labeled, large_labels)

    print("Вычисляем евклидово расстояние до ближайших крупных областей (DT)...")
    distances, nearest_large_indices = distance_transform_edt(~large_mask, return_indices=True)

    print("Перекрашиваем мелкие области в цвета их физических соседей...")
    small_mask = np.isin(labeled, small_labels)

    nearest_coords = tuple(nearest_large_indices[:, small_mask])
    labeled[small_mask] = labeled[nearest_coords]

    print("Перенумеровываем области...")
    new_labels = np.unique(labeled)
    new_labels = new_labels[new_labels != 0]
    new_labeled = np.zeros_like(labeled)
    for new_id, old_id in enumerate(new_labels, start=1):
        new_labeled[labeled == old_id] = new_id

    print(f"Готово. Осталось {len(new_labels)} областей.")
    return new_labeled


def build_label_correspondence(merged, original):
    print("Определяем соответствие новых меток исходным...")
    new_labels = np.unique(merged)
    new_labels = new_labels[new_labels != 0]

    mapping = {}
    for lbl in tqdm(new_labels, desc="Сопоставление"):
        mask = merged == lbl
        orig_values, counts = np.unique(original[mask], return_counts=True)
        if len(orig_values) > 0:
            best_orig = orig_values[np.argmax(counts)]
            mapping[lbl] = best_orig
        else:
            mapping[lbl] = 0
    return mapping


def save_label_table(labeled, mapping, output_csv, label_names=None):
    _names = label_names if label_names is not None else LABEL_NAMES
    print(f"Сохраняю таблицу меток в {output_csv}")
    labels = np.unique(labeled)
    counts = np.bincount(labeled.ravel())

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["New Label", "Original Label", "Name", "Voxel Count"])
        for lbl in labels:
            if lbl == 0:
                continue
            orig_lbl = mapping.get(lbl, 0)
            name = _names.get(orig_lbl, "Unknown")
            writer.writerow([lbl, orig_lbl, name, counts[lbl]])


def show_comparison_5slices(before, after, save_dir=None):
    if not _HAS_MPL:
        print("matplotlib недоступен, визуализация пропущена")
        return
    depth = before.shape[0]
    slices = np.linspace(0, depth - 1, 5, dtype=int)

    fig, axes = plt.subplots(2, 5, figsize=(20, 8))
    for i, s in enumerate(slices):
        axes[0, i].imshow(before[s], cmap="nipy_spectral", interpolation="nearest")
        axes[0, i].set_title(f"До (slice {s})")
        axes[0, i].axis("off")

        axes[1, i].imshow(after[s], cmap="nipy_spectral", interpolation="nearest")
        axes[1, i].set_title(f"После (slice {s})")
        axes[1, i].axis("off")

    plt.tight_layout()

    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        out = os.path.join(save_dir, "02_merge_slices.png")
        fig.savefig(out, dpi=100, bbox_inches="tight")
        print(f"График сохранён: {out}")

    if "agg" not in _matplotlib.get_backend().lower():
        plt.show(block=False)
        plt.pause(2)

    plt.close(fig)


if __name__ == "__main__":
    import sys as _sys
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument("--config", default="pipeline_config.json")
    _cfg_path = _parser.parse_args().config

    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # ── Читаем INFO.txt ───────────────────────────────────────────────
    info_txt_path = cfg.get("info_txt")
    _label_names_info = None
    _label_merges_info: dict[int, int] = {}
    z_start_mm_info = None
    z_step_mm_info = abs(float(cfg.get("z_step_mm", 1.0)))

    if info_txt_path and os.path.exists(info_txt_path):
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from parse_info_txt import parse_info_txt as _parse_info

        _info = _parse_info(info_txt_path)
        _label_names_info = _info.label_names
        _label_merges_info = _info.label_merges
        z_start_mm_info = _info.z_start_mm
        z_step_mm_info = _info.dz
        print(f"[INFO] {_info}")
    else:
        z_start_mm_info = cfg.get("z_start_mm")
        _label_merges_info = {int(k): int(v) for k, v in cfg.get("label_merges", {}).items()}

    input_path = os.path.join(cfg["output_dir"], "01_labeled.npy")
    original_path = cfg["input_volume"]
    output_path = os.path.join(cfg["output_dir"], "02_merged.npy")
    table_csv = os.path.join(cfg["output_dir"], "label_table.csv")
    min_voxels = cfg["min_voxels"]

    print(f"Загружаю данные из {input_path}...")
    labeled_volume = np.load(input_path)

    print(f"Загружаю исходную разметку из {original_path}...")
    original_volume = np.load(original_path)

    # ── Опциональная обрезка исходного объёма по Z ───────────────────
    z_cut_mm_cfg = cfg.get("z_cut_mm")
    if z_cut_mm_cfg is not None and z_start_mm_info is not None:
        z_cut_idx = int((float(z_cut_mm_cfg) - z_start_mm_info) / z_step_mm_info)
        print(f"Обрезка original_volume по Z: индекс {z_cut_idx}")
        original_volume = original_volume[z_cut_idx:, :, :]

    # ── Опциональная обрезка по Y ─────────────────────────────────────
    y_end = cfg.get("y_end_idx")
    if y_end is not None:
        print(f"Обрезка original_volume по Y: оставляем [:, :{y_end}, :]")
        original_volume = original_volume[:, : int(y_end), :]

    # ── Канонизация меток original_volume ─────────────────────────────
    # Этап 1 (connectivity_searcher) применяет слияния из INFO.txt ДО поиска
    # компонент. Мажоритарное восстановление ниже голосует по original_volume,
    # поэтому его тоже нужно привести к каноническим меткам — иначе слитая
    # (или занулённая в фон) метка может вернуться в итоговый объём.
    if _label_merges_info:
        for src, dst in _label_merges_info.items():
            original_volume[original_volume == src] = dst

    merged = merge_small_regions(labeled_volume, min_voxels=min_voxels)
    mapping = build_label_correspondence(merged, original_volume)

    # Восстанавливаем исходные метки тканей вместо последовательной нумерации
    restored = np.zeros_like(merged, dtype=merged.dtype)
    for new_lbl, orig_lbl in mapping.items():
        restored[merged == new_lbl] = orig_lbl

    plots_dir = os.path.join(cfg["output_dir"], "plots")
    show_comparison_5slices(labeled_volume, restored, save_dir=plots_dir)

    np.save(output_path, restored)
    print(f"Результат сохранён: {output_path}")

    # Свежий 02_merged делает бэкап label_smoother устаревшим — удаляем его,
    # чтобы повторное сглаживание считалось от актуального объёма (idempotency).
    _orig_backup = os.path.splitext(output_path)[0] + ".orig.npy"
    if os.path.exists(_orig_backup):
        os.remove(_orig_backup)
        print(f"Удалён устаревший бэкап сглаживания: {_orig_backup}")

    # Таблица: original label = new label (identity), т.к. метки исходные
    identity_mapping = {int(l): int(l) for l in np.unique(restored) if l != 0}
    save_label_table(restored, identity_mapping, table_csv, label_names=_label_names_info)
    print("Таблица меток сохранена.")
