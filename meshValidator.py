# -*- coding: utf-8 -*-
import vtk

# === БЛОКИРУЕМ ЛЮБЫЕ ОКНА С ОШИБКАМИ VTK ===
vtk.vtkObject.GlobalWarningDisplayOff()
try:
    vtk.vtkLogger.SetStderrVerbosity(vtk.vtkLogger.VERBOSITY_OFF)
except AttributeError:
    pass
# ===========================================

import os
import gc
import json
from concurrent.futures import ProcessPoolExecutor, as_completed

import logging
import tempfile  # был внутри функции — переносим на уровень модуля

import numpy as np
from vtkmodules.util.numpy_support import (
    vtk_to_numpy,
    numpy_to_vtk,
    numpy_to_vtkIdTypeArray,
    get_numpy_array_type,
)
import trimesh
import trimesh.proximity as proximity
import pymeshlab
from scipy.spatial import cKDTree
from tqdm import tqdm

import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning)

log = logging.getLogger(__name__)

SHIFT_DELTA = 0.5
DECIMATE = True

HIERARCHY = {
    3: 100,  # Мозг
    6: 10,  # Кожа
}


def get_priority(filename):
    try:
        num = int(filename.split("_")[1].split(".")[0])
        return HIERARCHY.get(num, 0)
    except:
        return 0


def vtk_to_trimesh(polydata):
    pts = polydata.GetPoints()
    if not pts or pts.GetNumberOfPoints() == 0:
        return trimesh.Trimesh()

    vertices = vtk_to_numpy(pts.GetData()).astype(np.float32)
    polys = polydata.GetPolys()
    if not polys or polys.GetNumberOfCells() == 0:
        return trimesh.Trimesh(vertices=vertices, process=False)

    faces_vtk = vtk_to_numpy(polys.GetData())
    faces = faces_vtk.reshape(-1, 4)[:, 1:4].astype(np.int32)

    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def trimesh_to_vtk(tmesh):
    polydata = vtk.vtkPolyData()

    if len(tmesh.vertices) > 0:
        points = vtk.vtkPoints()
        points.SetData(numpy_to_vtk(tmesh.vertices.astype(np.float32)))
        polydata.SetPoints(points)

    if len(tmesh.faces) > 0:
        vtk_id_type = get_numpy_array_type(vtk.VTK_ID_TYPE)
        faces_vtk = (
            np.column_stack((np.full(len(tmesh.faces), 3), tmesh.faces))
            .flatten()
            .astype(vtk_id_type)
        )
        cells = vtk.vtkCellArray()
        cells.SetCells(len(tmesh.faces), numpy_to_vtkIdTypeArray(faces_vtk))
        polydata.SetPolys(cells)

    return polydata


def load_mesh(path):
    reader = vtk.vtkPolyDataReader()
    reader.SetFileName(path)
    reader.ReadAllScalarsOff()
    reader.ReadAllVectorsOff()
    reader.ReadAllNormalsOff()
    reader.ReadAllTCoordsOff()
    reader.Update()
    return reader.GetOutput()


def save_mesh(mesh, path):
    writer = vtk.vtkPolyDataWriter()
    writer.SetFileName(path)
    writer.SetInputData(mesh)
    writer.Write()


def safe_get_volume(polydata):
    mass = vtk.vtkMassProperties()
    mass.SetInputData(polydata)
    mass.Update()
    return mass.GetVolume()


def fast_bbox_overlap(bounds1, bounds2):
    return np.all(bounds1[0] <= bounds2[1]) and np.all(bounds1[1] >= bounds2[0])


def fast_exact_collision(tmesh_a, tmesh_b, threshold=0.1):
    bounds_a = tmesh_a.bounds
    bounds_b = tmesh_b.bounds
    verts_a = tmesh_a.vertices
    verts_b = tmesh_b.vertices

    mask_a = np.all((verts_a >= bounds_b[0]) & (verts_a <= bounds_b[1]), axis=1)
    pts_a = verts_a[mask_a]
    if len(pts_a) == 0:
        return False

    mask_b = np.all((verts_b >= bounds_a[0]) & (verts_b <= bounds_a[1]), axis=1)
    pts_b = verts_b[mask_b]
    if len(pts_b) == 0:
        return False

    MAX_PTS = 50000
    if len(pts_a) > MAX_PTS:
        pts_a = pts_a[np.random.choice(len(pts_a), MAX_PTS, replace=False)]
    if len(pts_b) > MAX_PTS:
        pts_b = pts_b[np.random.choice(len(pts_b), MAX_PTS, replace=False)]

    tree = cKDTree(pts_b)
    distances, _ = tree.query(pts_a, k=1, distance_upper_bound=threshold)
    return bool(np.min(distances) < threshold)


def prepare_mesh_worker(args):
    in_path, temp_dir, decimated_dir, do_decimate, target_faces, calc_metrics = args
    name = os.path.basename(in_path)

    poly_orig = load_mesh(in_path)
    pts = poly_orig.GetPoints()
    if not pts or pts.GetNumberOfPoints() == 0:
        return name, None, None

    vol_orig = safe_get_volume(poly_orig)

    vertices = vtk_to_numpy(pts.GetData()).astype(np.float32)
    faces_vtk = vtk_to_numpy(poly_orig.GetPolys().GetData())
    faces = faces_vtk.reshape(-1, 4)[:, 1:4].astype(np.int32)
    faces_before = len(faces)

    original_samples = None
    if calc_metrics:
        if len(vertices) > 10000:
            sample_indices = np.random.choice(len(vertices), 10000, replace=False)
            original_samples = vertices[sample_indices].copy()
        else:
            original_samples = vertices.copy()

    try:
        m = pymeshlab.Mesh(vertices, faces)
        ms = pymeshlab.MeshSet()
        ms.add_mesh(m, "original")

        del poly_orig, vertices, faces_vtk, faces, m
        gc.collect()

        # МАГИЯ ОЧИСТКИ: Удаляем "космическую пыль" (все куски < 5000 полигонов)
        try:
            ms.meshing_remove_connected_components_by_face_number(mincomponentsize=5000)
        except:
            pass

        try:
            ms.meshing_remove_unreferenced_vertices()
        except:
            pass
        try:
            ms.meshing_remove_duplicate_faces()
        except:
            pass
        try:
            ms.meshing_remove_duplicate_vertices()
        except:
            pass

        if do_decimate and faces_before > target_faces:
            try:
                # ВОЗВРАЩАЕМ preservetopology=True, так как меш сглажен и очищен
                ms.meshing_decimation_quadric_edge_collapse(
                    targetfacenum=target_faces,
                    preservenormal=True,
                    preservetopology=True,
                    optimalplacement=True,
                )
                ms.meshing_surface_smooth_taubin(iterations=10)
            except:
                pass

        try:
            ms.meshing_close_holes(maxholesize=100)
        except:
            pass

        out_mesh = ms.current_mesh()
        processed_tmesh = trimesh.Trimesh(
            vertices=out_mesh.vertex_matrix(), faces=out_mesh.face_matrix(), process=False
        )

        ms.clear()
        del ms, out_mesh
        gc.collect()

    except Exception as e:
        print(f"Ошибка в PyMeshLab для {name}: {e}")
        return name, None, None

    faces_after = len(processed_tmesh.faces)
    max_err, rms_err = -1.0, -1.0

    if calc_metrics and original_samples is not None:
        try:
            _, distances, _ = proximity.closest_point(processed_tmesh, original_samples)
            max_err = float(np.max(distances))
            rms_err = float(np.sqrt(np.mean(distances**2)))
        except:
            pass

    poly_processed = trimesh_to_vtk(processed_tmesh)
    vol_proc = safe_get_volume(poly_processed)

    vol_diff_pct = abs(vol_orig - vol_proc) / vol_orig * 100.0 if vol_orig > 0 else 0.0

    metrics = {
        "name": name,
        "max_err": max_err,
        "rms_err": rms_err,
        "vol_diff_pct": vol_diff_pct,
        "faces_before": faces_before,
        "faces_after": faces_after,
    }

    backup_path = os.path.join(decimated_dir, name)
    save_mesh(poly_processed, backup_path)

    temp_path = os.path.join(temp_dir, name)
    save_mesh(poly_processed, temp_path)

    del processed_tmesh, poly_processed
    if original_samples is not None:
        del original_samples
    gc.collect()

    return name, temp_path, metrics


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument("--config", default="pipeline_config.json")
    _cfg_path = _parser.parse_args().config

    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    TARGET_FACES = cfg.get("target_faces", 100000)
    CALC_METRICS = cfg.get("calc_metrics", False)
    WORKERS = 1

    vtk_export_dir = os.path.join(cfg["output_dir"], "vtk_export")
    MESH_DIR = os.path.join(vtk_export_dir, "meshes")
    DECIMATED_DIR = os.path.join(vtk_export_dir, "meshes", "decimated")
    OUTPUT_DIR = os.path.join(vtk_export_dir, "validated")
    REPORT_PATH = os.path.join(OUTPUT_DIR, "report.txt")

    print("Working...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DECIMATED_DIR, exist_ok=True)

    mesh_files = sorted([f for f in os.listdir(MESH_DIR) if f.endswith(".vtk")])
    mesh_paths = [os.path.join(MESH_DIR, f) for f in mesh_files]

    print(f">> Мешей: {len(mesh_files)}. Потоков на обработку (защита от OOM): {WORKERS}")

    with tempfile.TemporaryDirectory() as temp_dir:
        mesh_dict = {}
        report_lines = ["========================================"]
        report_lines.append("STAGE 1: MESH PREPARATION & METRICS")
        report_lines.append("========================================\n")
        report_lines.append(
            f"{'Mesh Name':<15} | {'Faces (Before->After)':<22} | {'Max Error':<10} | {'RMS Error':<10} | {'Vol Loss %':<10}"
        )
        report_lines.append("-" * 78)

        print(
            f"\n>> Очистка, децимация и расчет метрик (PyMeshLab)... [Метрики: {'ВКЛ' if CALC_METRICS else 'ВЫКЛ'}]"
        )
        prep_tasks = [
            (p, temp_dir, DECIMATED_DIR, DECIMATE, TARGET_FACES, CALC_METRICS) for p in mesh_paths
        ]

        with ProcessPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(prepare_mesh_worker, t) for t in prep_tasks]
            for f in tqdm(as_completed(futures), total=len(futures), desc="Подготовка"):
                name, temp_path, metrics = f.result()
                if temp_path is None:
                    continue
                mesh_dict[name] = temp_path
                faces_str = f"{metrics['faces_before']} -> {metrics['faces_after']}"
                report_lines.append(
                    f"{name:<15} | {faces_str:<22} | {metrics['max_err']:<10.4f} | {metrics['rms_err']:<10.4f} | {metrics['vol_diff_pct']:<10.2f}%"
                )

        print("\n>> Загрузка мешей в RAM-кэш для быстрого доступа...")
        ram_cache = {}
        for name, path in tqdm(mesh_dict.items(), desc="Кэширование"):
            poly = load_mesh(path)
            tmesh = vtk_to_trimesh(poly)
            del poly  # VTK-объект больше не нужен до финального сохранения
            gc.collect()

            tmesh.merge_vertices()
            tmesh.update_faces(tmesh.nondegenerate_faces())
            tmesh.fix_normals()

            is_vol = tmesh.is_volume
            ram_cache[name] = {
                "tmesh": tmesh,
                "bounds": tmesh.bounds,
                "is_volume": is_vol,
                "volume": tmesh.volume if is_vol else 0.0,
            }

        names = list(mesh_dict.keys())
        report_lines.append("\n========================================")
        report_lines.append("STAGE 2: BOOLEAN OPERATIONS (In-Memory)")
        report_lines.append("========================================\n")

        print("\n>> Разрешение пересечений в памяти...")
        total_pairs = (len(names) * (len(names) - 1)) // 2

        with tqdm(total=total_pairs, desc="Анализ пар") as pbar:
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    name_a = names[i]
                    name_b = names[j]

                    data_a = ram_cache[name_a]
                    data_b = ram_cache[name_b]

                    if not fast_bbox_overlap(data_a["bounds"], data_b["bounds"]):
                        pbar.update(1)
                        continue

                    vol_a, vol_b = data_a["volume"], data_b["volume"]
                    prio_a = get_priority(name_a)
                    prio_b = get_priority(name_b)

                    if prio_a > prio_b:
                        tool_name, tool_data = name_a, data_a
                        target_name, target_data = name_b, data_b
                    elif prio_b > prio_a:
                        tool_name, tool_data = name_b, data_b
                        target_name, target_data = name_a, data_a
                    else:
                        if vol_a > vol_b:
                            tool_name, tool_data = name_a, data_a
                            target_name, target_data = name_b, data_b
                        else:
                            tool_name, tool_data = name_b, data_b
                            target_name, target_data = name_a, data_a

                    is_deep_intersection = False
                    action_taken = ""

                    try:
                        inter_mesh = tool_data["tmesh"].intersection(
                            target_data["tmesh"], engine="manifold"
                        )
                        if not inter_mesh.is_empty:
                            vol_int = inter_mesh.volume if inter_mesh.is_volume else 0.0
                            min_vol = min(vol_a, vol_b)
                            if min_vol == 0:
                                min_vol = 1e-9
                            is_deep_intersection = vol_int > (0.01 * min_vol)
                        else:
                            pbar.update(1)
                            continue
                    except:
                        is_deep_intersection = False

                    if not is_deep_intersection:
                        if not fast_exact_collision(tool_data["tmesh"], target_data["tmesh"]):
                            pbar.update(1)
                            continue

                    if is_deep_intersection:
                        try:
                            result_tmesh = target_data["tmesh"].difference(
                                tool_data["tmesh"], engine="manifold"
                            )
                            ram_cache[target_name]["tmesh"] = result_tmesh
                            ram_cache[target_name]["bounds"] = result_tmesh.bounds
                            ram_cache[target_name]["is_volume"] = result_tmesh.is_volume
                            ram_cache[target_name]["volume"] = (
                                result_tmesh.volume if result_tmesh.is_volume else 0.0
                            )
                            action_taken = (
                                f"Manifold Cut: {tool_name} отрезал кусок от {target_name}"
                            )
                        except:
                            is_deep_intersection = False

                    if not is_deep_intersection:
                        tmesh_t = tool_data["tmesh"]
                        tmesh_tar = target_data["tmesh"]
                        center_tool = (
                            tmesh_t.bounds.mean(axis=0)
                            if not tmesh_t.is_empty
                            else np.array([0, 0, 0])
                        )
                        center_target = (
                            tmesh_tar.bounds.mean(axis=0)
                            if not tmesh_tar.is_empty
                            else np.array([0, 0, 0])
                        )
                        vec = center_target - center_tool
                        mag = np.linalg.norm(vec)
                        vec = vec / mag if mag > 0 else np.array([1.0, 0.0, 0.0])
                        tmesh_tar.apply_translation(vec * SHIFT_DELTA)
                        ram_cache[target_name]["bounds"] = tmesh_tar.bounds
                        action_taken = (
                            f"Сдвиг {target_name} на {SHIFT_DELTA} (Легкое касание/Fallback)"
                        )

                    log_line = f"{name_a} <-> {name_b} | Действие: {action_taken}"
                    report_lines.append(log_line)
                    tqdm.write(log_line)
                    pbar.update(1)

        report_lines.append("\n========================================")
        report_lines.append("STAGE 3: FINAL MANIFOLD CHECKS")
        report_lines.append("========================================\n")
        report_lines.append(
            f"{'Mesh Name':<15} | {'Watertight (No leaks)':<25} | {'Euler Number':<12}"
        )
        report_lines.append("-" * 60)

        print("\n>> Дамп кэша: финальные проверки и сохранение на диск...")
        for name, data in tqdm(ram_cache.items(), desc="Сохранение"):
            tmesh = data["tmesh"]

            if not tmesh.is_watertight:
                try:
                    tmesh.fill_holes()
                except Exception as exc:
                    print(f"  [WARN] fill_holes для {name}: {exc}")

            # Конвертируем в VTK только здесь, один раз на меш
            final_vtk = trimesh_to_vtk(tmesh)
            final_path = os.path.join(OUTPUT_DIR, name)
            save_mesh(final_vtk, final_path)

            is_wt = tmesh.is_watertight
            euler = tmesh.euler_number
            status = "PASSED (Watertight)" if is_wt else "FAILED (Leaky)"
            report_lines.append(f"{name:<15} | {status:<25} | {euler:<12}")

            del final_vtk, tmesh
            data["tmesh"] = None  # освобождаем trimesh из кэша сразу после записи
            gc.collect()

        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))

    print("\n[OK] Готово! Открой report.txt для просмотра метрик и финального статуса.")
