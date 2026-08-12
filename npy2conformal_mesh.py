# -*- coding: utf-8 -*-
"""
Конформная генерация многоматериальных поверхностных сеток.

Tier A (default): vtkDiscreteFlyingEdges3D — обрабатывает весь том за один
проход, вершины на границах тканей совпадают точно. Нет зазоров. Быстро.

Tier B (--cgal): pygalmesh (CGAL) — настоящие конформные тет-сетки,
граничные треугольники являются общими. Нужен для MMC. Медленно.

Запуск:
    python npy2conformal_mesh.py                  # Tier A, из pipeline_config.json
    python npy2conformal_mesh.py --cgal           # Tier B
    python npy2conformal_mesh.py --max-radius 2.0 # размер тет в Tier B
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

# vtk импортируется лениво только в Tier A, чтобы скрипт запускался
# из cgal_env (Python 3.10), где VTK не установлен.

# Гарантируем, что Unicode-символы в логе (→, ─) не роняют StreamHandler на
# не-UTF-8 кодовой странице Windows (cp1251/cp1252) — иначе UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# Маппинг label → имя ткани (для именования файлов)
LABEL_NAMES: Dict[int, str] = {
    1: "CSF",
    2: "GrayMatter",
    3: "WhiteMatter",
    4: "Fat",
    5: "Muscle",
    6: "Skin",
    7: "Skull",
    8: "Vessels",
    9: "AroundFat",
    10: "DuraMatter",
    11: "BoneMarrow",
}


# ════════════════════════════════════════════════════════════════════════
#  Tier A — vtkDiscreteFlyingEdges3D (рекомендуется как стартовая точка)
# ════════════════════════════════════════════════════════════════════════


def _numpy_to_vtk_image(volume: np.ndarray, spacing: Tuple[float, float, float]):
    """Конвертирует 3-D numpy-массив в vtkImageData с правильным spacing."""
    import vtk  # noqa: PLC0415 — ленивый импорт, Tier A only
    from vtkmodules.util.numpy_support import numpy_to_vtk

    img = vtk.vtkImageData()
    img.SetDimensions(volume.shape[2], volume.shape[1], volume.shape[0])
    img.SetSpacing(spacing[2], spacing[1], spacing[0])
    flat = volume.ravel(order="C").astype(np.int16)
    vtk_arr = numpy_to_vtk(flat, deep=True, array_type=vtk.VTK_SHORT)
    vtk_arr.SetName("labels")
    img.GetPointData().SetScalars(vtk_arr)
    return img


def generate_conformal_surfaces_vtk(
    volume: np.ndarray,
    spacing: Tuple[float, float, float],
    labels: List[int],
    output_dir: str,
    smooth_iterations: int = 20,
    label_names: Optional[Dict[int, str]] = None,
) -> None:
    """
    Tier A: один запуск vtkDiscreteFlyingEdges3D → per-label поверхностные сетки.

    Ключевое свойство: все изоповерхности строятся из одного vtkImageData,
    поэтому вершины на границах тканей лежат в одних и тех же точках
    пространства. Зазоры между сетками исключены по построению.
    """
    import vtk  # noqa: PLC0415

    _lnames = label_names if label_names is not None else LABEL_NAMES
    log.info("Tier A: vtkDiscreteFlyingEdges3D по всему тому (%s вокселей)", volume.shape)
    img = _numpy_to_vtk_image(volume, spacing)

    dfe = vtk.vtkDiscreteFlyingEdges3D()
    dfe.SetInputData(img)
    for lbl in labels:
        dfe.SetValue(labels.index(lbl), lbl)
    dfe.ComputeNormalsOn()
    dfe.ComputeGradientsOff()
    dfe.Update()

    multi_block: vtk.vtkPolyData = dfe.GetOutput()
    del img
    gc.collect()

    log.info("Извлекаем %d поверхностей...", len(labels))
    for lbl in labels:
        name = _lnames.get(lbl, f"label_{lbl:02d}")
        log.info("  Обработка: %s (label=%d)", name, lbl)

        # Вырезаем только полигоны с нужным скалярным значением
        thresh = vtk.vtkThreshold()
        thresh.SetInputData(multi_block)
        thresh.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, "labels")
        thresh.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
        thresh.SetLowerThreshold(lbl - 0.5)
        thresh.SetUpperThreshold(lbl + 0.5)
        thresh.Update()

        surface = vtk.vtkDataSetSurfaceFilter()
        surface.SetInputConnection(thresh.GetOutputPort())
        surface.Update()

        poly: vtk.vtkPolyData = surface.GetOutput()
        if poly.GetNumberOfPoints() == 0:
            log.warning("  Пустая поверхность для label=%d, пропускаем", lbl)
            continue

        smoother = None
        if smooth_iterations > 0:
            smoother = vtk.vtkSmoothPolyDataFilter()
            smoother.SetInputData(poly)
            smoother.SetNumberOfIterations(smooth_iterations)
            smoother.SetRelaxationFactor(0.1)
            smoother.BoundarySmoothingOff()
            smoother.FeatureEdgeSmoothingOff()
            smoother.Update()
            poly = smoother.GetOutput()

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(poly)
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOn()
        normals.SplittingOff()
        normals.Update()

        out_path = os.path.join(output_dir, f"conformal_{lbl:02d}_{name}.vtk")
        writer = vtk.vtkPolyDataWriter()
        writer.SetFileName(out_path)
        writer.SetInputData(normals.GetOutput())
        writer.Write()
        log.info(
            "  Сохранено: %s  (%d вершин, %d полигонов)",
            out_path,
            normals.GetOutput().GetNumberOfPoints(),
            normals.GetOutput().GetNumberOfCells(),
        )

        del thresh, surface, normals, poly
        if smoother is not None:
            del smoother
        gc.collect()


# ════════════════════════════════════════════════════════════════════════
#  Tier B — pygalmesh (CGAL): настоящие конформные тет-сетки
# ════════════════════════════════════════════════════════════════════════


def generate_conformal_tet_mesh_cgal(
    volume: np.ndarray,
    spacing: Tuple[float, float, float],
    labels: List[int],
    output_dir: str,
    max_circumradius: float = 3.0,
    max_facet_distance: float = 0.5,
    max_facet_size: float = 0.0,
    target_faces: int = 100_000,
    label_names: Optional[Dict[int, str]] = None,
    smooth_iterations: int = 0,
) -> None:
    """
    Tier B: pygalmesh.generate_from_array() → конформная тет-сетка.

    CGAL строит единую сетку, в которой треугольные грани на границе
    двух тканей буквально общие (shared) — нет дублирующихся вершин,
    нет зазоров. Это формат, который понимает MCX/MMC напрямую.

    max_facet_size ограничивает размер треугольника НА ПОВЕРХНОСТИ
    (CGAL max_radius_surface_delaunay_ball) — критерий, независимый от
    max_cell_circumradius (размер тетраэдра) и max_facet_distance (допуск
    отклонения от вокселя). Без него у CGAL нет верхней границы на размер
    поверхностных фасетов → редкие крупные несимметричные треугольники
    ("прыщи"/спайки) на поверхности.

    ВАЖНО: в отличие от max_cell_circumradius, pygalmesh принимает это как
    ОДНО число на весь объём (per-label словарь не поддерживается) — значит
    оно давит одинаково и на мелкие ядра мозга, и на кожу/череп.

    Измерено на мышиных данных:
      • 4 ядра мозга (medulla/cerebellum/olfactory/cerebrum) отдельно,
        circumradius=0.3: facet_size=0.3 почти не действует (<1% изменений,
        уже покрыто cell_circumradius/facet_distance). facet_size=0.15
        (circumradius/2) реально связывает поверхность: p99/медиана
        (метрика "спайков") 1.59→1.45, максимальная грань 0.59→0.30мм —
        то, что нужно против прыщей на компактных структурах.
      • Те же 4 ядра + кожа/череп в одном прогоне, facet_size=0.15
        глобально: тети мозговых ядер выросли всего +59–64% (ожидаемо),
        но кожа/череп — ПОЛНАЯ сетка +263% тетов (кожа +327%, череп +302%),
        т.к. они получают грубый cell_circumradius (shell-heuristic ×2), но
        вынуждены давать мелкие поверхностные фасеты. Дорого для всей головы.

    Поэтому 0.0 (по умолчанию) = НЕ ограничиваем (старое поведение) — это
    параметр для точечного включения (CLI/config), а не тихий дефолт на
    полную мультитканевую сборку.

    Требует: pip install pygalmesh meshio
    """
    try:
        import pygalmesh  # type: ignore
        import meshio  # type: ignore
    except ImportError:
        log.error("Tier B требует pygalmesh и meshio: pip install pygalmesh meshio")
        sys.exit(1)

    # ── Защита от суб-воксельных параметров ──────────────────────────────
    # circumradius/facet_distance меньше размера вокселя заставляют CGAL плодить
    # миллионы тетраэдров → исчерпание памяти и краш 0xC0000005 (access violation).
    # Поднимаем до безопасного минимума вместо падения.
    min_sp = min(spacing)
    radius_floor = 2.0 * min_sp
    facet_floor = 1.0 * min_sp
    if max_circumradius < radius_floor:
        log.warning(
            "max_circumradius=%.3f мм слишком мал (< 2×воксель=%.3f мм) — риск взрыва "
            "памяти; поднимаю до %.3f мм",
            max_circumradius,
            radius_floor,
            radius_floor,
        )
        max_circumradius = radius_floor
    if max_facet_distance < facet_floor:
        log.warning(
            "max_facet_distance=%.3f мм слишком мал (< воксель=%.3f мм) — поднимаю " "до %.3f мм",
            max_facet_distance,
            facet_floor,
            facet_floor,
        )
        max_facet_distance = facet_floor

    if 0.0 < max_facet_size < facet_floor:
        log.warning(
            "max_facet_size=%.3f мм слишком мал (< воксель=%.3f мм) — поднимаю " "до %.3f мм",
            max_facet_size,
            facet_floor,
            facet_floor,
        )
        max_facet_size = facet_floor

    # ── Разрешение по тканям ─────────────────────────────────────────────
    # Крупные «оболочки» (кожа, череп) на мелком разрешении становятся
    # топологически шумными (много тоннелей) и теряют watertight; солидные
    # структуры (ядра мозга) выигрывают от детализации. Даём оболочкам вдвое
    # более грубый circumradius, остальным — заданный мелкий. Критерий — доля
    # вокселей (универсально, без привязки к именам тканей).
    counts = {lbl: int(np.count_nonzero(volume == lbl)) for lbl in labels}
    total_vox = sum(counts.values()) or 1
    radius_map = {
        lbl: (max_circumradius * 2.0 if counts[lbl] / total_vox > 0.10 else max_circumradius)
        for lbl in labels
    }
    coarse = [lbl for lbl in labels if radius_map[lbl] != max_circumradius]
    if coarse:
        log.info("  Грубее (оболочки, radius×2): метки %s", coarse)

    import os as _os

    n_threads = _os.cpu_count() or 1
    log.info("Tier B: pygalmesh — генерация конформной тет-сетки")
    log.info(
        "  spacing=%s  max_circumradius=%.2f  max_facet_distance=%.2f  max_facet_size=%.2f",
        spacing,
        max_circumradius,
        max_facet_distance,
        max_facet_size,
    )
    log.info("  perturb=False  exude=False  TBB threads=%d (автоматически)", n_threads)
    log.info("  Ожидаемое время: ~20–60 мин (зависит от circumradius и размера объёма)")

    # perturb и exude — пост-обработка для устранения sliver-тетраэдров.
    # Для MC-переноса света критична топология, не качество слайверов.
    # Exude в прошлом прогоне занял 17 мин и вернул CANT_IMPROVE_ANYMORE —
    # CGAL Delaunay и без них даёт достаточно хорошую сетку.
    mesh = pygalmesh.generate_from_array(
        volume.astype(np.uint16),
        [spacing[0], spacing[1], spacing[2]],
        max_cell_circumradius=radius_map,
        max_facet_distance=max_facet_distance,
        max_radius_surface_delaunay_ball=max_facet_size,
        min_facet_angle=25.0,
        perturb=False,
        exude=False,
        verbose=True,
        seed=0,
    )

    # Taubin-сглаживание на ВСЕХ вершинах тет-сетки (до извлечения поверхностей).
    # Общие вершины на границах тканей двигаются вместе → конформальность сохраняется.
    if smooth_iterations > 0:
        log.info("Taubin smoothing: %d итераций...", smooth_iterations)
        smoothed_pts = _smooth_tet_points(
            mesh.points,
            [cb.data for cb in mesh.cells],
            iterations=smooth_iterations,
        )
        mesh = meshio.Mesh(
            points=smoothed_pts,
            cells=mesh.cells,
            cell_data=mesh.cell_data,
            point_data=getattr(mesh, "point_data", {}),
        )

    # Сохраняем полную тет-сетку в формате Medit (.mesh) для MMC
    tet_path = os.path.join(output_dir, "brain_full_conformal.mesh")
    meshio.write(tet_path, mesh)
    log.info("Тет-сетка сохранена: %s", tet_path)

    # Экспортируем поверхностные сетки по меткам в VTK (со сглаживанием поверхностей)
    exported = _export_surface_from_tet(
        mesh,
        labels,
        output_dir,
        label_names=label_names,
        surface_smooth_iterations=smooth_iterations,
    )

    # Валидируем каждую поверхность, децимируем до target_faces и пишем report.txt
    validate_conformal_surfaces(exported, output_dir, target_faces=target_faces)

    # Финальное сглаживание «прыщей» на СВОБОДНЫХ (не-интерфейсных) вершинах
    # каждой поверхности; общие вершины соседних тканей заморожены → и
    # конформальность цела, и не возникает шипов на стыке fine/coarse.
    if smooth_iterations > 0:
        _despike_free_surfaces(exported, despike_iters=15)

    # Проверяем конформальность: соседние ткани должны делить общие грани
    _check_surface_conformality(exported, volume, output_dir, label_names=label_names)


def _despike_free_surfaces(
    vtk_paths: List[str],
    despike_iters: int = 15,
) -> None:
    """Убирает «прыщи» на СВОБОДНЫХ (не-интерфейсных) вершинах — уже записанных VTK.

    Идёт ПОСЛЕ валидации, поэтому pymeshfix/merge его не перетирают. Каждая
    поверхность сглаживается НЕЗАВИСИМО, со своим собственным Laplacian'ом и
    масштабом (средняя локальная длина ребра ЭТОЙ ткани) — никакого смешивания
    fine-мозг / coarse-кожа. Общие (интерфейсные) вершины соседних тканей
    ЗАМОРОЖЕНЫ (определяются по совпадению координат между поверхностями), поэтому
    стыки не двигаются → конформальность цела и на границах не растут шипы.
    Двигаются только единичные вершины, торчащие > половины локального ребра.
    """
    try:
        import meshio  # noqa: PLC0415
        import scipy.sparse as sp  # noqa: PLC0415
    except ImportError:
        return

    surfs = []  # [path, points, faces]
    for p in vtk_paths:
        m = meshio.read(p)
        tris = next((cb.data for cb in m.cells if cb.type == "triangle"), None)
        if tris is None or len(m.points) == 0:
            continue
        surfs.append([p, m.points.astype(np.float64), np.asarray(tris)])
    if len(surfs) < 1:
        return

    # Общие вершины = совпадающие координаты (round 1e-4) в ≥2 поверхностях.
    all_pts = np.vstack([s[1] for s in surfs])
    sid = np.concatenate([np.full(len(s[1]), i) for i, s in enumerate(surfs)])
    _uniq, inv = np.unique(np.round(all_pts, 4), axis=0, return_inverse=True)
    n_surf_per_gid: Dict[int, set] = {}
    for g, si in zip(inv.tolist(), sid.tolist()):
        n_surf_per_gid.setdefault(g, set()).add(si)
    shared_gid = np.array([len(n_surf_per_gid[g]) > 1 for g in range(len(_uniq))])

    total_moved = 0
    off = 0
    for p, P, T in surfs:
        k = len(P)
        free = ~shared_gid[inv[off : off + k]]  # свободные (не общие) вершины
        off += k
        if not free.any():
            continue
        # Собственный Laplacian поверхности
        rows_l, cols_l = [], []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            rows_l.extend((T[:, a], T[:, b]))
            cols_l.extend((T[:, b], T[:, a]))
        rows = np.concatenate(rows_l)
        cols = np.concatenate(cols_l)
        A = sp.csr_matrix((np.ones(len(rows), np.float32), (rows, cols)), shape=(k, k))
        A.data[:] = 1.0
        deg = np.asarray(A.sum(axis=1)).ravel()
        deg[deg == 0] = 1.0
        Lm = sp.diags(1.0 / deg) @ A

        def _max_curv(pp: np.ndarray) -> float:
            return float(np.linalg.norm(np.asarray(Lm @ pp) - pp, axis=1).max())

        pts = P.copy()
        moved_here = 0
        for _ in range(despike_iters):
            delta = np.asarray(Lm @ pts) - pts
            mag = np.linalg.norm(delta, axis=1)
            edge_len = np.linalg.norm(pts[rows] - pts[cols], axis=1)
            scale = np.bincount(rows, weights=edge_len, minlength=k) / np.maximum(
                np.bincount(rows, minlength=k), 1
            )
            spike = free & (mag > 0.5 * scale)
            if not spike.any():
                break
            moved_here += int(spike.sum())
            pts[spike] = pts[spike] + 0.8 * delta[spike]

        # Предохранитель: принимаем despike только если он НЕ ухудшил максимальную
        # кривизну поверхности (на грубых мешах, напр. коже, он может создать шипы).
        if moved_here and _max_curv(pts) <= _max_curv(P) + 1e-9:
            total_moved += moved_here
            meshio.write(p, meshio.Mesh(points=pts, cells=[meshio.CellBlock("triangle", T)]))
    log.info("Despike свободных вершин поверхностей: сдвигов=%d", total_moved)


def _check_surface_conformality(
    vtk_paths: List[str],
    volume: np.ndarray,
    output_dir: str,
    label_names: Optional[Dict[int, str]] = None,
) -> None:
    """Проверяет конформальность интерфейсов между соседними тканями.

    Для каждой пары меток, соседних по вокселям (6-связность), считает число
    буквально общих треугольников (совпадающие координаты) в их VTK-поверхностях.
    Пишет conformality_report.txt и предупреждает, если соседи НЕ делят граней —
    это разрыв интерфейса, критичный для Monte-Carlo (утечка фотонов).
    """
    try:
        import meshio  # noqa: PLC0415
        from scipy import ndimage  # noqa: PLC0415
    except ImportError:
        return

    names = label_names or {}
    lbl_path: Dict[int, str] = {}
    for p in vtk_paths:
        try:
            lbl_path[int(os.path.basename(p).split("_")[1])] = p
        except (IndexError, ValueError):
            continue
    labels = sorted(lbl_path)
    if len(labels) < 2:
        return

    # Воксельная смежность (6-связность → реальные интерфейсы, без диагоналей)
    touching: List[Tuple[int, int]] = []
    for i, a in enumerate(labels):
        dil = ndimage.binary_dilation(volume == a)
        for b in labels[i + 1 :]:
            if np.any(dil & (volume == b)):
                touching.append((a, b))

    def _tri_set(path: str) -> set:
        m = meshio.read(path)
        pts = m.points
        tris = next((cb.data for cb in m.cells if cb.type == "triangle"), None)
        out = set()
        if tris is not None:
            for t in tris:
                out.add(tuple(sorted(map(tuple, np.round(pts[t], 4).tolist()))))
        return out

    tsets = {lbl: _tri_set(p) for lbl, p in lbl_path.items()}

    lines = [
        "=" * 74,
        "ПРОВЕРКА КОНФОРМАЛЬНОСТИ (общие грани соседних тканей)",
        "=" * 74,
        f"{'интерфейс':<48}{'общих граней':>14}",
        "-" * 74,
    ]
    broken = 0
    for a, b in touching:
        shared = len(tsets[a] & tsets[b])
        pair = f"{names.get(a, str(a))} <-> {names.get(b, str(b))}"
        flag = "" if shared > 0 else "  ← НЕТ ОБЩИХ ГРАНЕЙ!"
        if shared == 0:
            broken += 1
        lines.append(f"{pair:<48}{shared:>14}{flag}")
    lines.append("=" * 74)
    lines.append(
        "Итог: конформно (все соседи делят грани)"
        if broken == 0
        else f"Итог: РАЗРЫВЫ у {broken} пар(ы) — см. выше"
    )

    report = os.path.join(output_dir, "conformality_report.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for ln in lines:
        log.info(ln)
    log.info("Отчёт конформальности: %s", report)


def _extract_region_surface(
    tet_data: np.ndarray,
    tet_labels: np.ndarray,
    lbl: int,
) -> Optional[np.ndarray]:
    """Извлекает замкнутую поверхность региона lbl из тетраэдральной сетки.

    Векторизованный алгоритм:
      1. Генерируем все 4 грани каждого тетраэдра региона → (N*4, 3).
      2. Сортируем вершины в каждой грани для канонической формы.
      3. Lex-sort → находим грани с count==1 (граничные).
      4. Для найденных граней берём ОРИГИНАЛЬНЫЙ порядок вершин (из тетраэдра),
         чтобы сохранить ориентацию (fix_normals исправит в validate).

    Возвращает граничные грани в глобальных индексах точек тет-сетки или None.
    """
    mask = tet_labels == lbl
    region_tets = tet_data[mask]
    if len(region_tets) == 0:
        return None

    # (N_tets, 4, 3) → (N_tets*4, 3)
    COMBOS = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.intp)
    all_faces_raw = region_tets[:, COMBOS].reshape(-1, 3)  # raw winding

    # Canonical (sorted per row) for counting
    all_faces_sorted = np.sort(all_faces_raw, axis=1)

    # Lex-sort by columns [2, 1, 0]
    lex_ord = np.lexsort(all_faces_sorted[:, ::-1].T)
    sorted_canon = all_faces_sorted[lex_ord]

    # diff[i] = True when row i+1 differs from row i
    diff = np.any(sorted_canon[1:] != sorted_canon[:-1], axis=1)
    starts = np.concatenate([[True], diff])  # start of each unique block
    ends = np.concatenate([diff, [True]])  # end   of each unique block
    # face appears exactly once ↔ it is both its own start AND end
    is_boundary = starts & ends

    orig_idx = lex_ord[is_boundary]
    boundary_faces = all_faces_raw[orig_idx].astype(np.int32)

    if len(boundary_faces) == 0:
        return None

    # Возвращаем грани в ГЛОБАЛЬНЫХ индексах точек тет-сетки (компактизация — в вызывающем коде).
    return boundary_faces


def _smooth_surface_points_global(
    points: np.ndarray,
    faces_list: List[np.ndarray],
    iterations: int = 20,
    lam: float = 0.5,
    mu: float = -0.53,
    despike_iters: int = 10,
) -> np.ndarray:
    """Taubin-сглаживание граничных вершин по связности ПОВЕРХНОСТИ.

    Работает в глобальном индексном пространстве тет-сетки: вершина, общая для
    поверхностей нескольких тканей, имеет один и тот же индекс → двигается один
    раз → конформальность (общие границы) сохраняется. Здесь нет тетраэдров, а
    значит нет guard'а от инверсии, который «замораживал» граничные вершины и
    оставлял шипы. Внутренние (не-граничные) точки не входят ни в одну грань →
    остаются на месте.
    """
    import scipy.sparse as sp  # noqa: PLC0415

    n = len(points)
    rows, cols = [], []
    for faces in faces_list:
        f = np.asarray(faces)
        for a, b in ((0, 1), (1, 2), (2, 0)):
            rows.extend((f[:, a], f[:, b]))
            cols.extend((f[:, b], f[:, a]))
    if not rows:
        return points
    rows = np.concatenate(rows)
    cols = np.concatenate(cols)
    A = sp.csr_matrix((np.ones(len(rows), np.float32), (rows, cols)), shape=(n, n))
    A.data[:] = 1.0  # бинарная матрица смежности (без веса дубликатов рёбер)
    deg = np.asarray(A.sum(axis=1)).ravel()
    moving = deg > 0  # только граничные вершины двигаются
    deg[~moving] = 1.0
    L = sp.diags(1.0 / deg) @ A
    orig = points.astype(np.float64)
    pts = orig.copy()
    for _ in range(iterations):
        pts = pts + lam * (L @ pts - pts)  # сжатие
        pts = pts + mu * (L @ pts - pts)  # анти-сжатие (|mu|>lam)
        pts[~moving] = orig[~moving]

    # ── Despike: добиваем «прыщи» ────────────────────────────────────────
    # Taubin — feature-preserving и оставляет единичные вершины с аномально
    # высокой кривизной. Критерий МАСШТАБНО-ОТНОСИТЕЛЬНЫЙ: вершина — шип, если её
    # отклонение от центроида соседей превышает долю ЛОКАЛЬНОЙ длины ребра. Так
    # порог адаптируется и к мелкому мозгу, и к грубой коже (глобальный порог не
    # работал: грубые ткани задирали медиану). Стягиваем только шипы, ровные
    # участки не трогаем. В глобальных индексах → конформальность цела.
    if despike_iters > 0 and moving.any():
        _n_moved = 0
        for _ in range(despike_iters):
            delta = np.asarray(L @ pts) - pts  # вектор к центроиду соседей
            mag = np.linalg.norm(delta, axis=1)
            edge_len = np.linalg.norm(pts[rows] - pts[cols], axis=1)
            scale = np.bincount(rows, weights=edge_len, minlength=n) / np.maximum(
                np.bincount(rows, minlength=n), 1
            )  # ср. длина ребра у вершины
            spike = moving & (mag > 0.5 * scale)  # торчит > половины локального ребра
            if not spike.any():
                break
            _n_moved += int(spike.sum())
            pts[spike] = pts[spike] + 0.8 * delta[spike]  # стянуть к центроиду
            pts[~moving] = orig[~moving]
        log.info("[DESPIKE] smoothed spike-vertex moves=%d", _n_moved)
    return pts


def _export_surface_from_tet(
    mesh,
    labels: List[int],
    output_dir: str,
    label_names: Optional[Dict[int, str]] = None,
    surface_smooth_iterations: int = 0,
) -> List[str]:
    """Извлекает замкнутые поверхностные сетки из тетраэдральной сетки.

    Вместо фильтрации по medit:ref (даёт незамкнутые поверхности из-за
    несогласованных ориентаций) используем тетраэдры каждого региона:
    граничная грань тетраэдра = грань, встречающаяся ровно один раз.
    Это даёт корректные замкнутые, watertight поверхности.

    Возвращает список путей к сохранённым VTK-файлам.
    """
    try:
        import meshio  # type: ignore
    except ImportError:
        return []

    points = mesh.points
    medit_refs: list = mesh.cell_data.get("medit:ref", [])

    # Собираем все тетраэдральные блоки с метками регионов
    tet_parts: List[Tuple[np.ndarray, np.ndarray]] = [
        (cb.data, ref) for cb, ref in zip(mesh.cells, medit_refs) if cb.type == "tetra" and len(ref)
    ]
    if not tet_parts:
        log.warning("_export_surface_from_tet: тетраэдральных блоков с метками не найдено.")
        return []

    tet_data = np.vstack([td for td, _ in tet_parts])
    tet_labels = np.concatenate([ref for _, ref in tet_parts])

    # pygalmesh/CGAL перенумеровывает субдомены последовательно по рангу
    # отсортированных значений меток: входные [1,2,4,5,6,7] → refs [1,2,3,4,5,6].
    # Возвращаем refs к оригинальным меткам, иначе поверхности будут перепутаны
    # и метки с «дырами» в нумерации (напр. 7) потеряются.
    present_refs = sorted({int(r) for r in np.unique(tet_labels)})
    sorted_labels = sorted({int(l) for l in labels})
    if len(present_refs) == len(sorted_labels) and present_refs != sorted_labels:
        ref_to_label = dict(zip(present_refs, sorted_labels))
        log.info("CGAL перенумеровал субдомены; маппинг ref→label: %s", ref_to_label)
        lut = np.arange(max(present_refs) + 1, dtype=tet_labels.dtype)
        for r, l in ref_to_label.items():
            lut[r] = l
        tet_labels = lut[tet_labels]

    _lnames = label_names if label_names is not None else LABEL_NAMES

    # 1) Извлекаем граничные грани каждого региона в ГЛОБАЛЬНЫХ индексах точек.
    region_faces: List[Tuple[int, str, np.ndarray]] = []
    for lbl in labels:
        name = _lnames.get(lbl, f"label_{lbl:02d}")
        faces = _extract_region_surface(tet_data, tet_labels, lbl)
        if faces is None:
            log.debug("  label=%d (%s): нет тетраэдров, пропускаем.", lbl, name)
            continue
        region_faces.append((lbl, name, faces))

    # 2) Конформное сглаживание поверхностей: общие вершинами регионов двигаются
    #    вместе (один глобальный индекс) → границы тканей остаются общими.
    # Сглаживание поверхностей делается ФИНАЛЬНЫМ проходом после валидации
    # (_smooth_final_surfaces_conformal) — иначе pymeshfix/merge на этапе валидации
    # его перетирают. Здесь только извлечение и запись сырых поверхностей.
    pts = points
    if surface_smooth_iterations > 0 and region_faces:
        pts = _smooth_surface_points_global(
            points,
            [f for _, _, f in region_faces],
            iterations=surface_smooth_iterations,
            despike_iters=0,  # despike — в финальном проходе
        )

    # 3) Компактизация индексов и запись каждой поверхности в VTK.
    saved: List[str] = []
    for lbl, name, faces in region_faces:
        used = np.unique(faces)
        remap = np.zeros(len(pts), dtype=np.int32)
        remap[used] = np.arange(len(used), dtype=np.int32)
        out_path = os.path.join(output_dir, f"surface_{lbl:02d}_{name}.vtk")
        meshio.write(
            out_path,
            meshio.Mesh(
                points=pts[used],
                cells=[meshio.CellBlock("triangle", remap[faces])],
            ),
        )
        log.info("  Поверхность label=%d (%s) → %s  (%d граней)", lbl, name, out_path, len(faces))
        saved.append(out_path)

    return saved


# ════════════════════════════════════════════════════════════════════════
#  Валидация поверхностных сеток (для Monte-Carlo совместимости)
# ════════════════════════════════════════════════════════════════════════


def _tet_signed_vol6(pts: np.ndarray, tet: np.ndarray) -> np.ndarray:
    """6× signed volume of every tetra; the sign encodes its orientation."""
    a, b, c, d = pts[tet[:, 0]], pts[tet[:, 1]], pts[tet[:, 2]], pts[tet[:, 3]]
    return np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)


def _smooth_tet_points(
    points: np.ndarray,
    cells_list: list,
    iterations: int = 20,
    lam: float = 0.5,
    mu: float = -0.53,
) -> np.ndarray:
    """Taubin smoothing on all tet mesh vertices (vectorised, sparse).

    Applied BEFORE surface extraction so shared boundary vertices move together,
    preserving conformality.  mu must satisfy |mu| > lam to prevent shrinkage.

    Inversion guard: a raw Taubin pass over an unconstrained boundary reliably
    flips the orientation of thin boundary tetrahedra (measured: 17k inverted
    tets on the CRISP brain mesh at 20 iters).  A negative-volume tet has an
    ill-defined inside/outside and is a hard blocker for Monte-Carlo photon
    transport.  After each iteration we therefore freeze — at their previous
    positions — every vertex incident to a tet that would flip or collapse,
    iterating that freeze to a fixed point so no new inversion is introduced.
    """
    import scipy.sparse as sp  # noqa: PLC0415

    n = len(points)
    tet = next((np.asarray(c) for c in cells_list if np.asarray(c).shape[1] == 4), None)

    rows_list, cols_list = [], []
    for cells in cells_list:
        cells = np.asarray(cells)
        k = cells.shape[1]  # vertices per cell (4 for tetra)
        for i in range(k):
            for j in range(k):
                if i != j:
                    rows_list.append(cells[:, i])
                    cols_list.append(cells[:, j])

    if not rows_list:
        return points

    rows = np.concatenate(rows_list).astype(np.int32)
    cols = np.concatenate(cols_list).astype(np.int32)
    A = sp.csr_matrix(
        (np.ones(len(rows), dtype=np.float32), (rows, cols)),
        shape=(n, n),
    )
    deg = np.asarray(A.sum(axis=1)).ravel()
    isolated = deg == 0  # points in no cell — keep them put, don't drift
    deg[isolated] = 1.0
    L = sp.diags(1.0 / deg) @ A  # row-normalised: L[i] = mean of neighbours

    pts = points.astype(np.float64)
    ref_sign = np.sign(_tet_signed_vol6(pts, tet)) if tet is not None else None

    frozen_last = 0
    for _ in range(iterations):
        nxt = pts + lam * (L @ pts - pts)  # shrink
        nxt = nxt + mu * (L @ nxt - nxt)  # anti-shrink
        nxt[isolated] = pts[isolated]

        if tet is not None:
            frozen = np.zeros(n, dtype=bool)
            for _guard in range(64):
                trial = np.where(frozen[:, None], pts, nxt)
                v6 = _tet_signed_vol6(trial, tet)
                bad = (np.sign(v6) != ref_sign) | (v6 == 0.0)
                if not bad.any():
                    break
                bad_verts = np.unique(tet[bad].ravel())
                if frozen[bad_verts].all():
                    break
                frozen[bad_verts] = True
            nxt = np.where(frozen[:, None], pts, nxt)
            frozen_last = int(frozen.sum())

        pts = nxt

    if tet is not None:
        n_bad = int(np.sum(np.sign(_tet_signed_vol6(pts, tet)) != ref_sign))
        log.info(
            "    Taubin smoothing done (%d iters); inverted tets=%d, frozen verts=%d",
            iterations,
            n_bad,
            frozen_last,
        )
    else:
        log.info("    Taubin smoothing done (%d iters)", iterations)
    return pts


def _decimate(tmesh, target_faces: int):
    """Децимация через fast-simplification (quadric error).

    Пропускается если target_faces <= 0.
    Возвращает (новый_меш, был_ли_изменён).
    """
    import trimesh  # noqa: PLC0415

    n = len(tmesh.faces)
    if target_faces <= 0 or n <= target_faces:
        return tmesh, False

    # target_reduction — доля граней для УДАЛЕНИЯ (0–1), как ожидает pyvista/fast-simplification.
    # Некоторые сборки trimesh передают первый позиционный аргумент напрямую в pyvista,
    # поэтому передаём float-ratio, а не целое число.
    target_reduction = max(0.01, min(0.99, 1.0 - target_faces / n))
    log.info(
        "    Децимация: %d → ~%d граней (target_reduction=%.3f)", n, target_faces, target_reduction
    )
    try:
        try:
            decimated = tmesh.simplify_quadric_decimation(target_reduction=target_reduction)
        except TypeError:
            decimated = tmesh.simplify_quadric_decimation(target_reduction)
        if len(decimated.faces) == 0:
            log.warning("    Децимация вернула пустой меш — используем оригинал.")
            return tmesh, False
        log.info("    Результат: %d граней", len(decimated.faces))
        return decimated, True
    except Exception as exc:
        log.warning("    Децимация не удалась (%s) — используем оригинал.", exc)
        return tmesh, False


def validate_conformal_surfaces(
    vtk_paths: List[str],
    output_dir: str,
    target_faces: int = 100_000,
) -> None:
    """
    Для каждого VTK из vtk_paths:
      1. Загружает через meshio → trimesh
      2. Децимирует до target_faces (OOM-защита для плотных CGAL-сеток)
      3. Проверяет watertight / Euler == 2 / число компонент
      4. Пытается зашить дыры (fill_holes) и перезаписывает файл
    Пишет validation_report.txt в output_dir.
    """
    try:
        import trimesh  # type: ignore
        import meshio  # type: ignore
        import gc
    except ImportError:
        log.error("Валидация требует trimesh и meshio.")
        return

    log.info("\n── ДЕЦИМАЦИЯ + ВАЛИДАЦИЯ ПОВЕРХНОСТНЫХ СЕТОК ──────────")
    log.info("   target_faces=%d (OOM-лимит на меш)", target_faces)

    lines: List[str] = [
        "=" * 105,
        "CGAL CONFORMAL MESH: DECIMATION + VALIDATION REPORT",
        "=" * 105,
        f"{'Mesh':<32} {'F_raw':>8} {'F_dec':>8}  {'Watertight':<16} {'Euler':<6} {'Comp':>5}"
        f" {'MaxErr(mm)':>12} {'RMSErr(mm)':>12} {'Vol%':>7}",
        "-" * 105,
    ]

    all_passed = True

    for vtk_path in vtk_paths:
        name = os.path.basename(vtk_path)
        try:
            mio = meshio.read(vtk_path)
            tri_data = next((cb for cb in mio.cells if cb.type == "triangle"), None)
            if tri_data is None or len(mio.points) == 0:
                log.warning("  %s: нет треугольных граней, пропускаем.", name)
                lines.append(
                    f"{name:<32} {'—':>8} {'—':>8}  {'EMPTY':<16} {'—':>6} {'—':>5} {'—':>12} {'—':>12} {'—':>7}"
                )
                continue

            faces_raw = len(tri_data.data)
            tmesh = trimesh.Trimesh(vertices=mio.points, faces=tri_data.data, process=False)
            del mio, tri_data
            gc.collect()

            tmesh.merge_vertices()
            tmesh.update_faces(tmesh.nondegenerate_faces())
            # Согласуем ориентацию граней и разворачиваем нормали наружу.
            # Обязательно после tet-based extraction (ориентация из тетраэдров
            # не всегда согласована).
            tmesh.fix_normals()

            # ── Сэмплирование ДО обработки (для полных метрик) ──────────
            # Сэмплируем сырой CGAL-меш — метрики покажут суммарное
            # изменение геометрии: repair + decimation вместе.
            _MAX_SAMPLES = 10_000
            _verts = tmesh.vertices
            if len(_verts) > _MAX_SAMPLES:
                _idx = np.random.default_rng(42).choice(len(_verts), _MAX_SAMPLES, replace=False)
                original_samples = _verts[_idx].copy()
            else:
                original_samples = _verts.copy()
            vol_before = tmesh.volume if tmesh.is_volume else 0.0
            del _verts

            # ── pymeshfix pass 1: repair before decimation ──────────────
            # Tet-based extraction creates non-manifold edges at "triple
            # lines" where 3 regions meet. pymeshfix (Attene 2010) resolves
            # these robustly; fill_holes alone cannot fix non-manifold meshes.
            if not tmesh.is_watertight:
                try:
                    import pymeshfix

                    _n_before = len(tmesh.faces)
                    _mf = pymeshfix.MeshFix(tmesh.vertices, tmesh.faces)
                    _mf.repair(joincomp=True)
                    if len(_mf.faces) > 0:
                        tmesh = trimesh.Trimesh(vertices=_mf.points, faces=_mf.faces, process=False)
                        del _mf
                        tmesh.merge_vertices()
                        log.info(
                            "    pymeshfix: %d → %d граней, watertight=%s",
                            _n_before,
                            len(tmesh.faces),
                            tmesh.is_watertight,
                        )
                    else:
                        del _mf
                        log.warning(
                            "    pymeshfix: убрал все %d граней → оставляем исходный меш",
                            _n_before,
                        )
                except Exception as exc:
                    log.warning("    pymeshfix: ошибка — %s", exc)

            # ── Децимация (OOM-guard) ────────────────────────────────────
            tmesh, was_decimated = _decimate(tmesh, target_faces)
            faces_dec = len(tmesh.faces)

            # ── pymeshfix pass 2: re-repair if decimation broke manifold ─
            # simplify_quadric_decimation does not guarantee manifold output.
            if not tmesh.is_watertight:
                try:
                    import pymeshfix

                    _n_pre2 = len(tmesh.faces)
                    _mf2 = pymeshfix.MeshFix(tmesh.vertices, tmesh.faces)
                    _mf2.repair(joincomp=True)
                    if len(_mf2.faces) > 0:
                        tmesh = trimesh.Trimesh(
                            vertices=_mf2.points, faces=_mf2.faces, process=False
                        )
                        del _mf2
                        tmesh.merge_vertices()
                        faces_dec = len(tmesh.faces)
                        log.info(
                            "    pymeshfix (post-decimate): %d → %d граней, watertight=%s",
                            _n_pre2,
                            faces_dec,
                            tmesh.is_watertight,
                        )
                    else:
                        del _mf2
                        log.warning("    pymeshfix (post-decimate): убрал все грани")
                except Exception as exc:
                    log.warning("    pymeshfix (post-decimate): ошибка — %s", exc)

            # ── Метрики: сырой CGAL-меш → финальный меш ─────────────────
            max_err: float = -1.0
            rms_err: float = -1.0
            vol_diff_pct: float = 0.0
            if len(original_samples) > 0 and len(tmesh.faces) > 0:
                try:
                    _, distances, _ = trimesh.proximity.closest_point(tmesh, original_samples)
                    max_err = float(np.max(distances))
                    rms_err = float(np.sqrt(np.mean(distances**2)))
                except Exception as exc:
                    log.warning("    Метрики (MaxErr/RMSErr): %s", exc)
            if vol_before > 0:
                vol_after = tmesh.volume if tmesh.is_volume else 0.0
                vol_diff_pct = abs(vol_before - vol_after) / vol_before * 100.0
            del original_samples

            # Перезаписываем файл (всегда — чтобы VTK был в sync с обработанным мешем)
            if was_decimated or True:  # always resave — ensures VTK is in sync
                meshio.write(
                    vtk_path,
                    meshio.Mesh(
                        points=tmesh.vertices,
                        cells=[meshio.CellBlock("triangle", tmesh.faces)],
                    ),
                )

            # ── Валидация ─────────────────────────────────────────────────
            is_wt = tmesh.is_watertight
            euler = tmesh.euler_number

            # trimesh.graph.connected_components требует networkx/scipy-engine,
            # которого может не быть в cgal_env. Используем scipy напрямую.
            try:
                import scipy.sparse
                import scipy.sparse.csgraph as _csgraph

                _e = tmesh.edges_unique
                _nv = len(tmesh.vertices)
                _r = np.concatenate([_e[:, 0], _e[:, 1]])
                _c = np.concatenate([_e[:, 1], _e[:, 0]])
                _adj = scipy.sparse.csr_matrix(
                    (np.ones(len(_r), dtype=np.float32), (_r, _c)),
                    shape=(_nv, _nv),
                )
                n_comp, _ = _csgraph.connected_components(_adj, directed=False)
            except Exception as _exc:
                log.warning("    connected_components через scipy: %s — n_comp=?", _exc)
                n_comp = -1

            status = "PASSED" if is_wt else "FAILED (leaky)"
            if not is_wt:
                all_passed = False

            max_err_str = f"{max_err:.4f}" if max_err >= 0 else "—"
            rms_err_str = f"{rms_err:.4f}" if rms_err >= 0 else "—"
            vol_str = f"{vol_diff_pct:.2f}" if vol_before > 0 else "—"
            lines.append(
                f"{name:<32} {faces_raw:>8} {faces_dec:>8}  {status:<16} {euler:<6} {n_comp:>5}"
                f" {max_err_str:>12} {rms_err_str:>12} {vol_str:>7}"
            )
            if n_comp > 1:
                lines.append(f"  ↳ WARNING: {n_comp} несвязных компонент")
                all_passed = False

            log.info(
                "  %-30s raw=%d dec=%d  wt=%-5s euler=%d comp=%d  maxErr=%s rmsErr=%s vol%%=%s",
                name,
                faces_raw,
                faces_dec,
                is_wt,
                euler,
                n_comp,
                max_err_str,
                rms_err_str,
                vol_str,
            )

        except Exception as exc:
            log.error("  %s: ошибка — %s", name, exc)
            lines.append(
                f"{name:<32} {'—':>8} {'—':>8}  {'ERROR':<16} {'—':>6} {'—':>5} {'—':>12} {'—':>12} {'—':>7}"
            )
            all_passed = False
        finally:
            try:
                del tmesh
            except NameError:
                pass
            gc.collect()

    lines.append("=" * 105)
    lines.append("Итог: " + ("ВСЕ СЕТКИ OK" if all_passed else "ЕСТЬ ПРОБЛЕМЫ — см. выше"))

    report_path = os.path.join(output_dir, "validation_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("Отчёт: %s", report_path)
    log.info("──────────────────────────────────────────────────────────")


# ════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Конформная генерация сеток из labeled .npy")
    parser.add_argument("--cgal", action="store_true", help="Использовать Tier B (pygalmesh/CGAL)")
    parser.add_argument(
        "--max-radius", type=float, default=None, help="CGAL: max_circumradius тет (мм)"
    )
    parser.add_argument(
        "--facet-dist", type=float, default=None, help="CGAL: max_facet_distance (мм)"
    )
    parser.add_argument(
        "--facet-size",
        type=float,
        default=None,
        help="CGAL: max_facet_size — размер поверхностного треугольника (мм)",
    )
    parser.add_argument(
        "--smooth", type=int, default=20, help="Iter сглаживания VTK (Tier A, 0=выкл)"
    )
    parser.add_argument(
        "--config", default="pipeline_config.json", help="Путь к pipeline_config.json"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    input_path: str = cfg["input_volume"]
    output_dir: str = os.path.join(cfg["output_dir"], "vtk_export", "conformal")
    os.makedirs(output_dir, exist_ok=True)

    # Читаем результат 02_merged.npy (после small_area_closer)
    merged_path = os.path.join(cfg["output_dir"], "02_merged.npy")
    log.info("Загрузка: %s", merged_path)
    volume: np.ndarray = np.load(merged_path)
    log.info("Размер: %s, dtype=%s", volume.shape, volume.dtype)

    present_labels: List[int] = [int(l) for l in np.unique(volume) if l != 0]
    log.info("Найденные метки: %s", present_labels)

    # ── Spacing и имена меток: INFO.txt → config → defaults ──────────
    info_txt_path = cfg.get("info_txt")
    _label_names_from_info: Optional[Dict[int, str]] = None
    if info_txt_path and os.path.exists(info_txt_path):
        from parse_info_txt import parse_info_txt as _parse_info

        _info = _parse_info(info_txt_path)
        spacing: Tuple[float, float, float] = _info.spacing_zyx  # (dz, dy, dx)
        _label_names_from_info = _info.label_names
        log.info("INFO.txt: %s", _info)
    else:
        z_step: float = abs(float(cfg.get("z_step_mm", 1.0)))
        y_step: float = abs(float(cfg.get("y_step_mm", z_step)))
        x_step: float = abs(float(cfg.get("x_step_mm", z_step)))
        spacing = (z_step, y_step, x_step)

    if args.cgal:
        # CLI аргументы перекрывают значения из конфига, если указаны
        max_radius = (
            args.max_radius
            if args.max_radius is not None
            else float(cfg.get("cgal_max_radius", 2.0))
        )
        facet_dist = (
            args.facet_dist
            if args.facet_dist is not None
            else float(cfg.get("cgal_facet_dist", 0.5))
        )
        facet_size = (
            args.facet_size
            if args.facet_size is not None
            else float(cfg.get("cgal_facet_size", 0.0))
        )
        target_faces = int(cfg.get("target_faces", 100_000))
        log.info("target_faces (OOM-лимит децимации): %d", target_faces)
        smooth_iters = int(cfg.get("cgal_smooth_iterations", 0))
        generate_conformal_tet_mesh_cgal(
            volume,
            spacing,
            present_labels,
            output_dir,
            max_circumradius=max_radius,
            max_facet_distance=facet_dist,
            max_facet_size=facet_size,
            target_faces=target_faces,
            label_names=_label_names_from_info,
            smooth_iterations=smooth_iters,
        )
    else:
        generate_conformal_surfaces_vtk(
            volume,
            spacing,
            present_labels,
            output_dir,
            smooth_iterations=args.smooth,
            label_names=_label_names_from_info,
        )

    log.info("Готово. Результаты в: %s", output_dir)
