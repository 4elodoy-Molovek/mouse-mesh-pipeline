# -*- coding: utf-8 -*-
"""
pipeline_manager.py — modern PySide6 front-end for the mesh pipeline.

This is the default GUI (needs PySide6); the legacy Tkinter version is kept as
pipeline_manager_tk.py. Same job: pick a config, set parameters,
run the CLI scripts (preprocessing -> mesh mode -> cleaning) and watch the log.
Improvements:
  * mode-aware panels — only the selected mesh mode's parameters are shown, so
    there are no inert fields (the old GUI showed classic-only knobs in every
    mode);
  * a live log console fed by QProcess;
  * a Metrics tab that loads surface_metrics' metrics.json into a table;
  * "Считать метрики" and "Сохранить рендеры" options for the envelope mode.

The pipeline scripts themselves are unchanged; this only orchestrates them with
the interpreter that runs this GUI (which carries all the deps).
"""

from __future__ import annotations

import codecs
import json
import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

MODES = [
    ("Оболочки (build_envelopes) — вложенные внешние оболочки тканей", "envelopes"),
    ("CGAL-remesh (C++) — Mesh_3 + тет-ремешинг + гладкие поверхности", "cgal-remesh"),
    ("Конформный CGAL (pygalmesh) — единая тет-сетка + валидация", "cgal"),
    ("Классический — npy2vtk + meshValidator (независимые изоповерхности)", "classic"),
]

# (config key, header label, tooltip). Tooltips are shown on the metrics table
# headers; the same explanations live in README "Метрики поверхностей".
METRIC_COLUMNS = [
    ("name", "ткань", "имя ткани (surface_NN_<ткань>)"),
    ("faces", "граней", "число треугольников поверхности"),
    ("area_mm2", "площадь см²", "площадь поверхности (см²)"),
    ("volume_mm3", "объём см³", "объём, заключённый внутри оболочки (см³)"),
    (
        "watertight",
        "wt",
        "watertight: замкнута, без граничных и non-manifold рёбер (норма Y, обязательно для MC)",
    ),
    (
        "genus",
        "genus",
        "топологический род = число сквозных тоннелей (0 = как сфера; "
        "у черепа >0 — анатомические форамины/швы, это норма)",
    ),
    ("components", "комп", "число связных компонент (норма 1)"),
    ("self_intersections", "self-x", "число самопересекающихся граней (норма 0)"),
    (
        "aspect_p99",
        "asp99",
        "99-й перцентиль aspect ratio треугольников (1 = равносторонний, большой = игла)",
    ),
    ("sliver_pct", "sliver%", "доля треугольников с минимальным углом < 10° (тонкие щепки), %"),
    (
        "rms_vox_mm",
        "RMSvox",
        "RMS-отклонение вершин от воксельной границы оболочки, мм (порядка вокселя = отлично)",
    ),
    ("max_vox_mm", "MAXvox", "максимальное отклонение от воксельной границы оболочки, мм"),
    ("rms_raw_mm", "RMSraw", "RMS-отклонение от сырой CGAL-сетки (до Taubin/децимации), мм"),
    ("hausdorff_raw_mm", "Hraw", "симметричное расстояние Хаусдорфа до сырой CGAL-сетки, мм"),
    (
        "min_gap_mm",
        "minGap",
        "минимальный зазор до родительской оболочки, мм (>0 = внутри; корень n/a)",
    ),
    ("pokethrough_pct", "poke%", "доля вершин снаружи родителя (торчание), % — норма 0.000"),
]

LIGHT = {
    "bg": "#f3f5f8",
    "panel": "#ffffff",
    "text": "#1b2733",
    "subtext": "#4a5568",
    "border": "#cbd3dd",
    "input": "#ffffff",
    "accent": "#2b6cb0",
    "accent_hi": "#2c5f96",
    "btn": "#eef1f5",
    "btn_hi": "#e2e8f0",
    "log_bg": "#0f1720",
    "log_fg": "#d7e2ee",
    "header": "#eef1f5",
    "table": "#ffffff",
    "grid": "#eef1f5",
    "disabled": "#9db4cc",
}
DARK = {
    "bg": "#171b21",
    "panel": "#1f242c",
    "text": "#e5ecf3",
    "subtext": "#9aa7b5",
    "border": "#333c47",
    "input": "#262d36",
    "accent": "#4a90d9",
    "accent_hi": "#5aa0e9",
    "btn": "#2a323c",
    "btn_hi": "#333c47",
    "log_bg": "#0d1218",
    "log_fg": "#cdd8e4",
    "header": "#262d36",
    "table": "#1f242c",
    "grid": "#2a323c",
    "disabled": "#3a4552",
}
THEMES = {"Светлая": LIGHT, "Тёмная": DARK}


def _qss(p):
    return f"""
QWidget {{ font-family: 'Segoe UI', system-ui; font-size: 13px; color: {p['text']}; }}
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p['bg']}; }}
QLabel {{ background: transparent; color: {p['text']}; }}
QLabel#status {{ color: {p['subtext']}; }}
QGroupBox {{
    background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 10px;
    margin-top: 14px; padding: 10px 12px 12px 12px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {p['accent']}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
    border-radius: 7px; padding: 4px 8px;
    selection-background-color: {p['accent']}; selection-color: white;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {p['accent']}; }}
QComboBox QAbstractItemView {{
    background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
    selection-background-color: {p['accent']}; selection-color: white; outline: 0;
}}
QPushButton {{
    background: {p['btn']}; color: {p['text']}; border: 1px solid {p['border']};
    border-radius: 7px; padding: 5px 12px;
}}
QPushButton:hover {{ background: {p['btn_hi']}; }}
QPushButton#run {{ background: {p['accent']}; color: white; border: none; font-weight: 700; padding: 8px 18px; }}
QPushButton#run:hover {{ background: {p['accent_hi']}; }}
QPushButton#run:disabled {{ background: {p['disabled']}; color: #e8eef5; }}
QPlainTextEdit {{
    background: {p['log_bg']}; color: {p['log_fg']}; border: none; border-radius: 8px;
    font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 12px;
}}
QTableWidget {{
    background: {p['table']}; color: {p['text']}; border: 1px solid {p['border']};
    border-radius: 8px; gridline-color: {p['grid']};
}}
QHeaderView::section {{ background: {p['header']}; color: {p['text']}; border: none; padding: 6px; font-weight: 600; }}
QTableCornerButton::section {{ background: {p['header']}; border: none; }}
QCheckBox {{ background: transparent; color: {p['text']}; spacing: 7px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {p['subtext']}; padding: 7px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ border-bottom: 2px solid {p['accent']}; color: {p['accent']}; font-weight: 600; }}
QScrollBar:vertical {{ background: {p['bg']}; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: {p['bg']}; height: 12px; margin: 0; }}
QScrollBar::handle {{ background: {p['border']}; border-radius: 6px; min-height: 24px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""


def _dspin(lo, hi, step, dec, val):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setDecimals(dec)
    w.setValue(val)
    return w


def _ispin(lo, hi, val):
    w = QtWidgets.QSpinBox()
    w.setRange(lo, hi)
    w.setValue(val)
    return w


class PipelineManager(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mouse Mesh Pipeline")
        self.resize(1180, 840)
        self._proc: QtCore.QProcess | None = None
        self._queue: list[dict] = []
        self._fields: dict[str, tuple] = {}
        self._settings = QtCore.QSettings("mouse-mesh-pipeline", "pipeline_manager")
        self._dec = None

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_settings())
        splitter.addWidget(self._build_output())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 620])
        self.setCentralWidget(splitter)
        self._update_mode()

        saved = self._settings.value("theme", "Светлая")
        self.theme_combo.setCurrentText(saved if saved in THEMES else "Светлая")
        self._apply_theme(self.theme_combo.currentText())

        self.py_edit.setText(self._settings.value("python", "", type=str))
        self.py_edit.editingFinished.connect(
            lambda: self._settings.setValue("python", self.py_edit.text().strip())
        )

    # ── settings panel ──────────────────────────────────────────────────────

    def _build_settings(self):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        host = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(host)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # 0. config
        g0 = QtWidgets.QGroupBox("Конфигурация")
        f0 = QtWidgets.QGridLayout(g0)
        self.cfg_path = QtWidgets.QLineEdit()
        f0.addWidget(QtWidgets.QLabel("Конфиг (.json):"), 0, 0)
        f0.addWidget(self.cfg_path, 0, 1)
        b_load = QtWidgets.QPushButton("Загрузить")
        b_save = QtWidgets.QPushButton("Сохранить")
        b_browse = QtWidgets.QPushButton("Обзор…")
        b_load.clicked.connect(self.load_config)
        b_save.clicked.connect(self.save_config)
        b_browse.clicked.connect(lambda: self._browse_into(self.cfg_path, "file"))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(b_browse)
        row.addWidget(b_load)
        row.addWidget(b_save)
        f0.addLayout(row, 1, 1)
        f0.addWidget(QtWidgets.QLabel("Python (env):"), 2, 0)
        self.py_edit = QtWidgets.QLineEdit()
        self.py_edit.setPlaceholderText(PY + "  (пусто = этот же python)")
        self.py_edit.setToolTip(
            "Интерпретатор для запуска скриптов пайплайна. Укажите python окружения,\n"
            "где стоят vtk/pymeshlab/meshio/trimesh (иначе шаги упадут на отсутствии\n"
            "зависимостей). Пусто = тот python, которым запущен сам GUI."
        )
        py_browse = QtWidgets.QPushButton("Обзор…")
        py_browse.clicked.connect(lambda: self._browse_into(self.py_edit, "file"))
        pyrow = QtWidgets.QHBoxLayout()
        pyrow.addWidget(self.py_edit, 1)
        pyrow.addWidget(py_browse)
        f0.addLayout(pyrow, 2, 1)
        v.addWidget(g0)

        # 1. inputs
        g1 = QtWidgets.QGroupBox("Входные данные и пути")
        f1 = QtWidgets.QGridLayout(g1)
        self.input_volume = self._path_row(f1, 0, "Объём (.npy):", "file")
        self.info_txt = self._path_row(f1, 1, "INFO.txt:", "file")
        self.output_dir = self._path_row(f1, 2, "Папка вывода:", "dir")
        self._reg("input_volume", self.input_volume.text, self.input_volume.setText, "")
        self._reg("info_txt", self.info_txt.text, self.info_txt.setText, "")
        self._reg("output_dir", self.output_dir.text, self.output_dir.setText, "")
        v.addWidget(g1)

        # 2. preprocessing
        g2 = QtWidgets.QGroupBox("Предобработка")
        f2 = QtWidgets.QGridLayout(g2)
        self.z_cut = QtWidgets.QLineEdit()
        self.y_end = QtWidgets.QLineEdit()
        self.min_voxels = _ispin(0, 10_000_000, 100)
        self.sigma = _dspin(0.0, 10.0, 0.5, 2, 2.0)
        self.workers = _ispin(1, 64, 6)
        self._grid(f2, 0, "Срез по Z (мм, пусто=выкл):", self.z_cut)
        self._grid(f2, 1, "Обрезка по Y (индекс, пусто=выкл):", self.y_end)
        self._grid(f2, 2, "Отбраковка (мин. вокселей):", self.min_voxels)
        self._grid(f2, 3, "Сглаживание меток σ (воксель):", self.sigma)
        self._grid(f2, 4, "Потоков:", self.workers)
        self.st1 = QtWidgets.QCheckBox("Этап 1: связные компоненты")
        self.st2 = QtWidgets.QCheckBox("Этап 2: фильтрация мелких областей")
        self.st3 = QtWidgets.QCheckBox("Этап 3: воксельное сглаживание меток")
        for c in (self.st1, self.st2, self.st3):
            c.setChecked(True)
            f2.addWidget(c, f2.rowCount(), 0, 1, 2)
        self._reg_opt("z_cut_mm", self.z_cut, float)
        self._reg_opt("y_end_idx", self.y_end, int)
        self._reg("min_voxels", self.min_voxels.value, self.min_voxels.setValue, 100)
        self._reg("label_smooth_sigma", self.sigma.value, self.sigma.setValue, 2.0)
        self._reg("workers", self.workers.value, self.workers.setValue, 6)
        v.addWidget(g2)

        # 3. mode
        g3 = QtWidgets.QGroupBox("Режим генерации сеток")
        f3 = QtWidgets.QVBoxLayout(g3)
        self.mode = QtWidgets.QComboBox()
        for label, _ in MODES:
            self.mode.addItem(label)
        self.mode.currentIndexChanged.connect(self._update_mode)
        f3.addWidget(self.mode)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self._page_envelopes())
        self.stack.addWidget(self._page_remesh())
        self.stack.addWidget(self._page_cgal())
        self.stack.addWidget(self._page_classic())
        f3.addWidget(self.stack)
        v.addWidget(g3)

        # native binary (envelopes + cgal-remesh)
        self.g_native = QtWidgets.QGroupBox("Нативный бинарник CGAL")
        fn = QtWidgets.QGridLayout(self.g_native)
        self.remesh_exe = self._path_row(fn, 0, "mesh_and_remesh.exe:", "file")
        self.msys2_bin = self._path_row(fn, 1, "MSYS2 ucrt64\\bin (или пусто):", "dir")
        self._reg("remesh_exe", self.remesh_exe.text, self.remesh_exe.setText, "")
        self._reg("msys2_bin", self.msys2_bin.text, self.msys2_bin.setText, "")
        v.addWidget(self.g_native)

        v.addStretch(1)
        scroll.setWidget(host)
        return scroll

    # ── per-mode pages ──────────────────────────────────────────────────────

    def _page_envelopes(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QGridLayout(w)
        f.setContentsMargins(0, 6, 0, 0)
        self.env_facet = _dspin(0.01, 5.0, 0.05, 3, 0.5)
        self.env_dist = _dspin(0.01, 5.0, 0.05, 3, 0.4)
        self.env_cell = _dspin(0.05, 10.0, 0.1, 3, 1.5)
        self.env_taubin = _ispin(0, 300, 40)
        self.env_seal = QtWidgets.QCheckBox("Запечатывать сквозные тоннели (genus→0)")
        self.env_seal.setChecked(True)
        self.env_seal_r = _ispin(0, 10, 2)
        self.env_nest = _ispin(0, 20, 2)
        self.env_jobs = _ispin(0, 64, 1)
        self.env_decim = _dspin(0.0, 1.0, 0.05, 2, 0.5)
        self._grid(f, 0, "Facet size (мм):", self.env_facet)
        self._grid(f, 1, "Facet distance (мм):", self.env_dist)
        self._grid(f, 2, "Cell size (мм, объёмная сетка):", self.env_cell)
        self._grid(f, 3, "Taubin (сглаживание):", self.env_taubin)
        f.addWidget(self.env_seal, 4, 0, 1, 2)
        self._grid(f, 5, "Радиус открытия (запечатка):", self.env_seal_r)
        self._grid(f, 6, "Запас вложенности (воксели):", self.env_nest)
        self._grid(f, 7, "Параллельных задач (0=авто):", self.env_jobs)
        self._grid(f, 8, "Децимация (доля, 0.5=50%, 0=выкл):", self.env_decim)
        self.env_metrics = QtWidgets.QCheckBox("Считать метрики поверхностей (surface_metrics)")
        self.env_render = QtWidgets.QCheckBox("Сохранить рендеры (6 видов + разрез)")
        f.addWidget(self.env_metrics, 9, 0, 1, 2)
        f.addWidget(self.env_render, 10, 0, 1, 2)
        self.env_gw = QtWidgets.QCheckBox("Разделить мозг на серое/белое (эрозия)")
        self.env_gw_mm = _dspin(0.1, 5.0, 0.1, 2, 0.6)
        f.addWidget(self.env_gw, 11, 0, 1, 2)
        self._grid(f, 12, "    Толщина серого (мм):", self.env_gw_mm)
        for key, w_ in (
            ("envelope_facet_size", self.env_facet),
            ("envelope_facet_dist", self.env_dist),
            ("envelope_cell_size", self.env_cell),
            ("envelope_decimate", self.env_decim),
        ):
            self._reg(key, w_.value, w_.setValue, w_.value())
        for key, w_ in (
            ("envelope_taubin", self.env_taubin),
            ("envelope_seal_radius", self.env_seal_r),
            ("envelope_nest_margin", self.env_nest),
            ("envelope_jobs", self.env_jobs),
        ):
            self._reg(key, w_.value, w_.setValue, w_.value())
        self._reg("envelope_seal_tunnels", self.env_seal.isChecked, self.env_seal.setChecked, True)
        self._reg(
            "envelope_metrics", self.env_metrics.isChecked, self.env_metrics.setChecked, False
        )
        self._reg("envelope_render", self.env_render.isChecked, self.env_render.setChecked, False)
        self._reg("grey_white_split", self.env_gw.isChecked, self.env_gw.setChecked, False)
        self._reg("gw_grey_mm", self.env_gw_mm.value, self.env_gw_mm.setValue, 0.6)
        return w

    def _page_remesh(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QGridLayout(w)
        f.setContentsMargins(0, 6, 0, 0)
        self.rm_facet = _dspin(0.01, 5.0, 0.05, 3, 0.5)
        self.rm_dist = _dspin(0.01, 5.0, 0.05, 3, 0.25)
        self.rm_cell = _dspin(0.05, 10.0, 0.1, 3, 1.5)
        self.rm_edge = _dspin(0.01, 5.0, 0.05, 3, 0.4)
        self.rm_iter = _ispin(0, 20, 2)
        self.rm_do = QtWidgets.QCheckBox("Тет-ремешинг (иначе только Mesh_3, ~14× быстрее)")
        self.rm_manifold = QtWidgets.QCheckBox("Критерий manifold (жрёт память)")
        self._grid(f, 0, "Facet size (мм):", self.rm_facet)
        self._grid(f, 1, "Facet distance (мм):", self.rm_dist)
        self._grid(f, 2, "Cell size (мм):", self.rm_cell)
        self._grid(f, 3, "Target edge (мм):", self.rm_edge)
        self._grid(f, 4, "Итераций ремешинга:", self.rm_iter)
        f.addWidget(self.rm_do, 5, 0, 1, 2)
        f.addWidget(self.rm_manifold, 6, 0, 1, 2)
        self.surf_clean = QtWidgets.QCheckBox("Чистка поверхностей (заливка + Taubin + нормали)")
        self.surf_clean.setChecked(True)
        self.surf_taubin = _ispin(0, 300, 40)
        self.surf_minf = _ispin(0, 1_000_000, 1000)
        self.export_mmc = QtWidgets.QCheckBox("Экспорт MMC (.node/.elem)")
        f.addWidget(self.surf_clean, 7, 0, 1, 2)
        self._grid(f, 8, "Surface Taubin:", self.surf_taubin)
        self._grid(f, 9, "Мин. граней компонента:", self.surf_minf)
        f.addWidget(self.export_mmc, 10, 0, 1, 2)
        for key, w_ in (
            ("remesh_facet_size", self.rm_facet),
            ("remesh_facet_dist", self.rm_dist),
            ("remesh_cell_size", self.rm_cell),
            ("remesh_target_edge", self.rm_edge),
        ):
            self._reg(key, w_.value, w_.setValue, w_.value())
        self._reg("remesh_iterations", self.rm_iter.value, self.rm_iter.setValue, 2)
        self._reg("remesh_do_remesh", self.rm_do.isChecked, self.rm_do.setChecked, False)
        self._reg("remesh_manifold", self.rm_manifold.isChecked, self.rm_manifold.setChecked, False)
        self._reg("surface_clean", self.surf_clean.isChecked, self.surf_clean.setChecked, True)
        self._reg("surface_taubin", self.surf_taubin.value, self.surf_taubin.setValue, 40)
        self._reg("surface_min_faces", self.surf_minf.value, self.surf_minf.setValue, 1000)
        self._reg("export_mmc", self.export_mmc.isChecked, self.export_mmc.setChecked, False)
        return w

    def _page_cgal(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QGridLayout(w)
        f.setContentsMargins(0, 6, 0, 0)
        self.cgal_radius = _dspin(0.01, 10.0, 0.05, 3, 0.3)
        self.cgal_dist = _dspin(0.01, 10.0, 0.05, 3, 0.1)
        self._grid(f, 0, "Max radius (мм):", self.cgal_radius)
        self._grid(f, 1, "Facet distance (мм):", self.cgal_dist)
        self._reg("cgal_max_radius", self.cgal_radius.value, self.cgal_radius.setValue, 0.3)
        self._reg("cgal_facet_dist", self.cgal_dist.value, self.cgal_dist.setValue, 0.1)
        return w

    def _page_classic(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QGridLayout(w)
        f.setContentsMargins(0, 6, 0, 0)
        self.target_faces = _ispin(0, 100_000_000, 999999)
        self.smooth_mc = QtWidgets.QCheckBox("Анти-алиасинг (Smooth Marching Cubes)")
        self.smooth_mc.setChecked(True)
        self.calc_metrics = QtWidgets.QCheckBox("Считать метрики Max/RMS (медленно)")
        self._grid(f, 0, "Целевое число полигонов:", self.target_faces)
        f.addWidget(self.smooth_mc, 1, 0, 1, 2)
        f.addWidget(self.calc_metrics, 2, 0, 1, 2)
        self._reg("target_faces", self.target_faces.value, self.target_faces.setValue, 999999)
        self._reg("use_smooth_mc", self.smooth_mc.isChecked, self.smooth_mc.setChecked, True)
        self._reg("calc_metrics", self.calc_metrics.isChecked, self.calc_metrics.setChecked, False)
        return w

    # ── output panel ────────────────────────────────────────────────────────

    def _build_output(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 12, 12, 12)
        bar = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("Запустить")
        self.run_btn.setObjectName("run")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QtWidgets.QPushButton("Стоп")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        self.status = QtWidgets.QLabel("готов")
        self.status.setObjectName("status")
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(list(THEMES.keys()))
        self.theme_combo.currentTextChanged.connect(self._apply_theme)
        bar.addWidget(self.run_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.status, 1)
        bar.addWidget(QtWidgets.QLabel("Тема:"))
        bar.addWidget(self.theme_combo)
        v.addLayout(bar)

        self.tabs = QtWidgets.QTabWidget()
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.table = QtWidgets.QTableWidget(0, len(METRIC_COLUMNS))
        self.table.setHorizontalHeaderLabels([c[1] for c in METRIC_COLUMNS])
        for c, col in enumerate(METRIC_COLUMNS):
            self.table.horizontalHeaderItem(c).setToolTip(col[2])  # hover = metric explanation
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabs.addTab(self.log, "Лог")
        self.tabs.addTab(self.table, "Метрики")
        v.addWidget(self.tabs, 1)
        return w

    # ── small helpers ───────────────────────────────────────────────────────

    def _grid(self, layout, row, label, widget):
        layout.addWidget(QtWidgets.QLabel(label), row, 0)
        layout.addWidget(widget, row, 1)

    def _path_row(self, layout, row, label, kind):
        le = QtWidgets.QLineEdit()
        btn = QtWidgets.QPushButton("Обзор…")
        btn.clicked.connect(lambda: self._browse_into(le, kind))
        layout.addWidget(QtWidgets.QLabel(label), row, 0)
        layout.addWidget(le, row, 1)
        layout.addWidget(btn, row, 2)
        return le

    def _browse_into(self, line, kind):
        if kind == "dir":
            p = QtWidgets.QFileDialog.getExistingDirectory(self, "Папка", line.text())
        else:
            p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Файл", line.text())
        if p:
            line.setText(p)

    def _reg(self, key, getter, setter, default):
        self._fields[key] = (getter, setter, default)

    def _reg_opt(self, key, line_edit, cast):
        def getter():
            t = line_edit.text().strip()
            if not t:
                return None
            try:
                return cast(float(t)) if cast is int else cast(t)
            except ValueError:
                return None

        def setter(v):
            line_edit.setText("" if v is None else str(v))

        self._fields[key] = (getter, setter, None)

    def _update_mode(self):
        idx = self.mode.currentIndex()
        self.stack.setCurrentIndex(idx)
        key = MODES[idx][1]
        self.g_native.setVisible(key in ("envelopes", "cgal-remesh"))

    def _apply_theme(self, name):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(_qss(THEMES.get(name, LIGHT)))
        self._settings.setValue("theme", name)

    def _py(self):
        """Interpreter used to run the pipeline scripts (env python if set)."""
        return self.py_edit.text().strip() or sys.executable

    # ── config load / save ──────────────────────────────────────────────────

    def load_config(self):
        path = self.cfg_path.text().strip()
        if not path or not os.path.exists(path):
            self._log("[конфиг] файл не найден", "warn")
            return
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key, (_, setter, default) in self._fields.items():
            setter(cfg.get(key, default))
        m = cfg.get("mesh_mode")
        if m:
            for i, (_, k) in enumerate(MODES):
                if k == m:
                    self.mode.setCurrentIndex(i)
        self._update_mode()
        self._log(f"[конфиг] загружен: {path}")

    def save_config(self):
        path = self.cfg_path.text().strip()
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить конфиг", "", "*.json")
            if not path:
                return
            self.cfg_path.setText(path)
        cfg = {}
        if os.path.exists(path):  # preserve envelope_parents and unknown keys
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        for key, (getter, _, _) in self._fields.items():
            cfg[key] = getter()
        cfg["mesh_mode"] = MODES[self.mode.currentIndex()][1]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        self._log(f"[конфиг] сохранён: {path}")

    # ── run ─────────────────────────────────────────────────────────────────

    def _val(self, key):
        return self._fields[key][0]()

    def _build_tasks(self):
        cfg = self.cfg_path.text().strip()
        if not cfg:
            self._log("[ошибка] укажите файл конфига", "warn")
            return []
        self.save_config()  # persist current widgets so scripts read them
        global PY
        PY = self._py()  # run scripts with the chosen env interpreter
        ca = ["--config", cfg]
        tasks = []
        if self.st1.isChecked():
            tasks.append(("connectivity_searcher", [PY, "connectivity_searcher.py"] + ca))
        if self.st2.isChecked():
            tasks.append(("small_area_closer", [PY, "small_area_closer.py"] + ca))
        if self.st3.isChecked():
            tasks.append(("label_smoother", [PY, "label_smoother.py"] + ca))

        mode = MODES[self.mode.currentIndex()][1]
        out_dir = self.output_dir.text().strip()
        if mode == "envelopes":
            if self.env_gw.isChecked():  # split brain -> grey shell + white core first
                tasks.append(("grey_white_split", [PY, "grey_white_split.py"] + ca))
            argv = (
                [PY, "build_envelopes.py"]
                + ca
                + [
                    "--facet-size",
                    str(self._val("envelope_facet_size")),
                    "--facet-distance",
                    str(self._val("envelope_facet_dist")),
                    "--cell-size",
                    str(self._val("envelope_cell_size")),
                    "--taubin",
                    str(self._val("envelope_taubin")),
                    "--seal-open-radius",
                    str(self._val("envelope_seal_radius")),
                    "--nest-margin",
                    str(self._val("envelope_nest_margin")),
                    "--jobs",
                    str(self._val("envelope_jobs")),
                    "--decimate",
                    str(self._val("envelope_decimate")),
                ]
            )
            if not self.env_seal.isChecked():
                argv.append("--no-seal-tunnels")
            if self.remesh_exe.text().strip():
                argv += ["--exe", self.remesh_exe.text().strip()]
            if self.msys2_bin.text().strip():
                argv += ["--msys2-bin", self.msys2_bin.text().strip()]
            if self.env_metrics.isChecked():
                argv.append("--keep-work")  # so surface_metrics finds exact region + raw mesh
            tasks.append(("build_envelopes", argv))
            if self.env_metrics.isChecked():
                tasks.append(("surface_metrics", [PY, "surface_metrics.py"] + ca))
            if self.env_render.isChecked():
                tasks.append(("render_surfaces", [PY, "render_surfaces.py"] + ca))
        elif mode == "cgal-remesh":
            tasks += self._remesh_tasks(ca, out_dir)
        elif mode == "cgal":
            argv = (
                [PY, "npy2conformal_mesh.py"]
                + ca
                + [
                    "--cgal",
                    "--max-radius",
                    str(self._val("cgal_max_radius")),
                    "--facet-dist",
                    str(self._val("cgal_facet_dist")),
                ]
            )
            tasks.append(("npy2conformal_mesh", argv))
        else:  # classic
            tasks.append(("npy2vtk", [PY, "npy2vtk.py"] + ca))
            tasks.append(("meshValidator", [PY, "meshValidator.py"] + ca))
        return tasks

    def _remesh_tasks(self, ca, out_dir):
        conformal = os.path.join(out_dir, "vtk_export", "conformal")
        inr = os.path.join(conformal, "volume.inr")
        mesh = os.path.join(conformal, "brain_full_conformal.mesh")
        surfaces = os.path.join(conformal, "surfaces")
        exe = self.remesh_exe.text().strip()
        msys = self.msys2_bin.text().strip()
        tasks = [("npy2inr", [PY, os.path.join("cgal_remesh", "npy2inr.py")] + ca + ["--out", inr])]
        exe_args = [
            exe,
            inr,
            mesh,
            "--facet-size",
            str(self._val("remesh_facet_size")),
            "--facet-distance",
            str(self._val("remesh_facet_dist")),
            "--cell-size",
            str(self._val("remesh_cell_size")),
        ]
        if self.rm_do.isChecked():
            exe_args += [
                "--target-edge-length",
                str(self._val("remesh_target_edge")),
                "--iterations",
                str(self._val("remesh_iterations")),
            ]
        else:
            exe_args.append("--no-remesh")
        env = None
        if msys and os.path.isdir(msys):
            env = {"PATH": msys + os.pathsep + os.environ.get("PATH", "")}
        tasks.append(("mesh_and_remesh", exe_args, env))
        mc = ca + ["--export-surfaces", surfaces, "--props"]
        if self.export_mmc.isChecked():
            mc.append("--export-mmc")
        tasks.append(("mc_mesh_check", [PY, "mc_mesh_check.py"] + mc))
        if self.surf_clean.isChecked():
            tasks.append(
                (
                    "surface_cleaner",
                    [
                        PY,
                        "surface_cleaner.py",
                        "--dir",
                        surfaces,
                        "--taubin",
                        str(self._val("surface_taubin")),
                        "--min-faces",
                        str(self._val("surface_min_faces")),
                    ],
                )
            )
        return tasks

    def run(self):
        if self._proc is not None:
            return
        tasks = self._build_tasks()
        if not tasks:
            return
        self._queue = [
            {"label": t[0], "argv": t[1], "env": (t[2] if len(t) > 2 else None)} for t in tasks
        ]
        self.log.clear()
        self.tabs.setCurrentIndex(0)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log(f"[пайплайн] {len(self._queue)} шаг(ов)\n")
        self._start_next()

    def _start_next(self):
        if not self._queue:
            self._finish(ok=True)
            return
        step = self._queue.pop(0)
        self.status.setText(f"выполняется: {step['label']}")
        self._log(f"\n=== {step['label']} ===\n$ " + " ".join(step["argv"]) + "\n")
        p = QtCore.QProcess(self)
        p.setWorkingDirectory(HERE)
        p.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        if os.name == "nt" and hasattr(p, "setCreateProcessArgumentsModifier"):
            # no flashing consoles for child scripts when the GUI runs under pythonw
            def _no_window(a):
                a.flags |= 0x08000000  # CREATE_NO_WINDOW

            p.setCreateProcessArgumentsModifier(_no_window)
        pe = QtCore.QProcessEnvironment.systemEnvironment()
        pe.insert("PYTHONUTF8", "1")  # force child stdout to UTF-8 so Cyrillic is readable
        pe.insert("PYTHONIOENCODING", "utf-8")
        if step["env"]:
            for k, val in step["env"].items():
                pe.insert(k, val)
        p.setProcessEnvironment(pe)
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        p.setProgram(step["argv"][0])
        p.setArguments(step["argv"][1:])
        p.readyReadStandardOutput.connect(lambda: self._read(p))
        p.finished.connect(lambda code, _st: self._step_done(code))
        p.errorOccurred.connect(lambda e: self._log(f"[QProcess] {e}", "warn"))
        self._proc = p
        p.start()

    def _read(self, p):
        raw = bytes(p.readAllStandardOutput())
        if not raw:
            return
        data = self._dec.decode(raw) if self._dec else raw.decode("utf-8", "replace")
        if data:
            self.log.moveCursor(QtGui.QTextCursor.End)
            self.log.insertPlainText(data)
            self.log.moveCursor(QtGui.QTextCursor.End)

    def _step_done(self, code):
        if self._proc:
            self._read(self._proc)
        self._proc = None
        if code != 0:
            self._log(f"\n[стоп] шаг завершился с кодом {code}", "warn")
            self._finish(ok=False)
            return
        self._start_next()

    def _finish(self, ok):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("готово" if ok else "остановлено (ошибка)")
        if ok:
            self._log("\n[пайплайн] готово")
        self._load_metrics()

    def stop(self):
        self._queue = []
        if self._proc:
            self._proc.kill()
        self._log("\n[стоп] прервано пользователем", "warn")

    def _load_metrics(self):
        out_dir = self.output_dir.text().strip()
        jp = os.path.join(out_dir, "vtk_export", "conformal", "surfaces_envelopes", "metrics.json")
        if not os.path.exists(jp):
            return
        try:
            rows = json.load(open(jp, encoding="utf-8"))
        except Exception:
            return
        self.table.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            for c, (key, _, _) in enumerate(METRIC_COLUMNS):
                v = rec.get(key)
                if key == "area_mm2" and isinstance(v, (int, float)):
                    v = f"{v/100:.1f}"
                elif key == "volume_mm3" and isinstance(v, (int, float)):
                    v = f"{v/1000:.1f}"
                elif key == "watertight":
                    v = "✓" if v else "×"
                elif isinstance(v, float):
                    v = f"{v:.3f}"
                item = QtWidgets.QTableWidgetItem("n/a" if v is None else str(v))
                item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.table.setItem(r, c, item)
        self.tabs.setCurrentIndex(1)
        self._log(f"[метрики] загружено {len(rows)} строк в таблицу")

    def _log(self, msg, level="info"):
        self.log.moveCursor(QtGui.QTextCursor.End)
        self.log.insertPlainText(msg + "\n")
        self.log.moveCursor(QtGui.QTextCursor.End)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = PipelineManager()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
