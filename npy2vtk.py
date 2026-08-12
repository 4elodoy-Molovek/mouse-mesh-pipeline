# -*- coding: utf-8 -*-
import vtk

# === БЛОКИРУЕМ ЛЮБЫЕ ОКНА С ОШИБКАМИ VTK ===
vtk.vtkObject.GlobalWarningDisplayOff()
try:
    vtk.vtkLogger.SetStderrVerbosity(vtk.vtkLogger.VERBOSITY_OFF)
except AttributeError:
    pass
# ===========================================

import numpy as np
import csv
import os
import json
import gc
from scipy.ndimage import binary_fill_holes
from vtkmodules.util import numpy_support


def save_mesh_from_mask(mask, spacing, out_path, use_smooth=True):
    print("   [INFO] Заливка внутренних пустот (Hole Filling) на уровне вокселей...")
    # МАГИЯ: превращаем "губку" в монолитный блок, заливая внутренние микро-пузырьки
    solid_mask = binary_fill_holes(mask)

    padded_mask = np.pad(solid_mask, pad_width=1, mode="constant", constant_values=0)

    if use_smooth:
        padded_mask = padded_mask.astype(np.float32)
        vtk_type = vtk.VTK_FLOAT
    else:
        padded_mask = padded_mask.astype(np.uint8)
        vtk_type = vtk.VTK_UNSIGNED_CHAR

    vtk_data = numpy_support.numpy_to_vtk(
        num_array=padded_mask.ravel(order="C"), deep=True, array_type=vtk_type
    )

    img = vtk.vtkImageData()
    img.SetDimensions(padded_mask.shape[::-1])  # (nx, ny, nz) — VTK order
    img.SetSpacing(spacing[0], spacing[1], spacing[2])  # (sx, sy, sz) in VTK = (dx, dy, dz)
    img.SetOrigin(-spacing[0], -spacing[1], -spacing[2])
    img.GetPointData().SetScalars(vtk_data)

    mc = vtk.vtkFlyingEdges3D()

    if use_smooth:
        print("   [INFO] Применяется Гауссово сглаживание...")
        gaussian = vtk.vtkImageGaussianSmooth()
        gaussian.SetInputData(img)
        gaussian.SetStandardDeviations(1.0, 1.0, 1.0)
        gaussian.SetRadiusFactors(2.0, 2.0, 2.0)
        gaussian.Update()
        mc.SetInputConnection(gaussian.GetOutputPort())

        # Захватываем истончившиеся размытые стенки
        mc.SetValue(0, 0.3)
    else:
        print("   [INFO] Применяется классический алгоритм без сглаживания...")
        mc.SetInputData(img)
        mc.SetValue(0, 0.5)

    mc.Update()

    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputConnection(mc.GetOutputPort())
    cleaner.Update()

    writer = vtk.vtkPolyDataWriter()
    writer.SetFileName(out_path)
    writer.SetInputData(cleaner.GetOutput())
    writer.Write()

    del mc, cleaner, img, vtk_data, padded_mask, solid_mask
    if use_smooth:
        del gaussian
    gc.collect()


def load_table(csv_path):
    new_labels = []
    map_new_to_orig = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            new_label = int(row["New Label"])
            orig_label = int(row["Original Label"])
            new_labels.append(new_label)
            map_new_to_orig[new_label] = orig_label
    return new_labels, map_new_to_orig


def export_meshes_by_new(volume, new_labels, out_dir, spacing, use_smooth):
    os.makedirs(out_dir, exist_ok=True)
    print("\n=== Меши по NEW label ===")
    for new_lbl in new_labels:
        print(f">> Mesh new {new_lbl}")
        mask = (volume == new_lbl).astype(np.uint8)
        out_path = os.path.join(out_dir, f"new_{new_lbl:03d}.vtk")

        save_mesh_from_mask(mask, spacing, out_path, use_smooth)

        print(f"   [OK] Сохранено: {out_path}")
        del mask
        gc.collect()


def export_all(volume_path, table_path, output_dir, spacing=(1, 1, 1), use_smooth=True):
    print("Загружаю том...")
    vol = np.load(volume_path)

    print("Загружаю таблицу...")
    new_labels, map_new_to_orig = load_table(table_path)

    meshes_dir = os.path.join(output_dir, "meshes")
    export_meshes_by_new(vol, new_labels, meshes_dir, spacing, use_smooth)
    print("\n[OK] Готово!")


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument("--config", default="pipeline_config.json")
    _cfg_path = _parser.parse_args().config

    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    merged_npy = os.path.join(cfg["output_dir"], "02_merged.npy")
    table_csv = os.path.join(cfg["output_dir"], "label_table.csv")
    vtk_export_dir = os.path.join(cfg["output_dir"], "vtk_export")

    use_smooth = cfg.get("use_smooth_mc", True)

    # spacing для VTK: (sx, sy, sz) = (x_spacing, y_spacing, z_spacing)
    # Приоритет: INFO.txt → отдельные *_step_mm → 1.0 мм.
    info_txt_path = cfg.get("info_txt")
    if info_txt_path and os.path.exists(info_txt_path):
        import sys as _sys

        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from parse_info_txt import parse_info_txt as _parse_info

        _info = _parse_info(info_txt_path)
        x_step, y_step, z_step = _info.dx, _info.dy, _info.dz
        print(f"[INFO] {_info}")
    else:
        z_step = abs(float(cfg.get("z_step_mm", 1.0)))
        x_step = abs(float(cfg.get("x_step_mm", z_step)))
        y_step = abs(float(cfg.get("y_step_mm", z_step)))
    export_all(
        volume_path=merged_npy,
        table_path=table_csv,
        output_dir=vtk_export_dir,
        spacing=(x_step, y_step, z_step),
        use_smooth=use_smooth,
    )
