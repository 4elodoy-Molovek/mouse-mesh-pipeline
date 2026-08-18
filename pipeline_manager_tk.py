# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

CONFIG_FILE = "pipeline_config.json"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Шаги предобработки — всегда как чекбоксы.
# (script, подпись, включён_по_умолчанию)
PRE_STEPS: list[tuple[str, str, bool]] = [
    ("connectivity_searcher.py", "Этап 1: Поиск связных компонент", True),
    ("small_area_closer.py", "Этап 2: Фильтрация мелких областей", True),
    ("label_smoother.py", "Этап 3: Сглаживание меток (воксельное)", False),
]

# Режимы генерации сеток: какие скрипты (для простых, только-python режимов).
# Режим "cgal-remesh" собирается вручную в _build_task_list (нужен нативный .exe).
MESH_MODES: dict[str, list[str]] = {
    "classic": ["npy2vtk.py", "meshValidator.py"],
    "cgal": ["npy2conformal_mesh.py"],
    "cgal-remesh": [],  # см. _build_cgal_remesh_tasks
}

_CGAL_SCRIPT = "npy2conformal_mesh.py"
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
# The native binary is called mesh_and_remesh.exe on Windows and mesh_and_remesh
# elsewhere. Packaged builds (deb/rpm/AppImage/pkg) export MMP_REMESH_EXE so the
# GUI points at the bundled binary regardless of where it was installed/mounted.
_EXE_SUFFIX = ".exe" if os.name == "nt" else ""
_DEFAULT_REMESH_EXE = os.environ.get("MMP_REMESH_EXE") or os.path.join(
    _REPO_DIR, "cgal_remesh", "mesh_and_remesh" + _EXE_SUFFIX
)
_DEFAULT_MSYS2_BIN = r"D:\msys64\ucrt64\bin" if os.name == "nt" else ""


class PipelineApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Medical Mesh Pipeline Manager")
        # Форма прокручивается, поэтому окно может быть ниже экрана; ограничиваем
        # высоту доступной областью, чтобы кнопки и консоль всегда были видны.
        _sh = self.root.winfo_screenheight()
        self.root.geometry(f"860x{min(940, max(600, _sh - 120))}")
        self.root.minsize(720, 560)

        self.max_cores = max(1, (os.cpu_count() or 4) - 2)

        # --- Файл конфигурации (можно переключать между датасетами) ---
        self.config_path_var = tk.StringVar(value=CONFIG_FILE)

        # --- Пути ---
        self.input_vol_var = tk.StringVar()
        self.minc_info_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()

        # --- Параметры предобработки ---
        # z_cut и y_end — строки: пустое значение = обрезка отключена (null).
        self.z_cut_var = tk.StringVar(value="-40.0")
        self.y_end_idx_var = tk.StringVar(value="")
        self.min_voxels_var = tk.IntVar(value=10000)
        self.target_faces_var = tk.IntVar(value=100000)
        self.use_smooth_mc_var = tk.BooleanVar(value=True)
        self.calc_metrics_var = tk.BooleanVar(value=False)
        self.workers_var = tk.IntVar(value=1)

        # --- Этапы предобработки ---
        self.pre_step_vars: list[tk.BooleanVar] = [
            tk.BooleanVar(value=default) for _, _, default in PRE_STEPS
        ]

        # --- Режим генерации сеток ---
        self.mesh_mode_var = tk.StringVar(value="classic")

        # --- Воксельное сглаживание меток (Этап 3) ---
        self.label_smooth_sigma_var = tk.DoubleVar(value=2.0)

        # --- Параметры CGAL (pygalmesh, режим "cgal") ---
        self.cgal_max_radius_var = tk.DoubleVar(value=3.0)
        self.cgal_facet_dist_var = tk.DoubleVar(value=0.5)
        self.cgal_smooth_var = tk.IntVar(value=20)
        _default_cgal_python = r"C:\Users\4elodoy Molovek\.conda\envs\cgal_env\python.exe"
        self.cgal_python_var = tk.StringVar(value=_default_cgal_python)

        # --- Параметры CGAL-remesh (C++ mesh_and_remesh, режим "cgal-remesh") ---
        self.rm_facet_size_var = tk.DoubleVar(value=0.4)
        self.rm_facet_dist_var = tk.DoubleVar(value=0.15)
        self.rm_cell_size_var = tk.DoubleVar(value=0.6)
        self.rm_target_edge_var = tk.DoubleVar(value=0.4)
        self.rm_iterations_var = tk.IntVar(value=2)
        self.rm_manifold_var = tk.BooleanVar(value=False)
        # Тетраэдральный ремешинг — последовательный и медленный. Для surface-MC
        # он избыточен (сглаживание делает surface_cleaner) → выкл = ~14× быстрее.
        self.rm_do_remesh_var = tk.BooleanVar(value=False)
        self.rm_exe_var = tk.StringVar(value=_DEFAULT_REMESH_EXE)
        self.rm_msys2_bin_var = tk.StringVar(value=_DEFAULT_MSYS2_BIN)

        # --- Параметры режима "envelopes" (вложенные оболочки для surface-MC) ---
        self.env_facet_var = tk.DoubleVar(value=0.10)
        self.env_dist_var = tk.DoubleVar(value=0.05)
        self.env_taubin_var = tk.IntVar(value=30)
        # Запечатывание сквозных «арок»-тоннелей (напр. у глаза) → genus 0, без
        # выпирающих заплаток. Применяется к корневым тканям (кожа).
        self.env_seal_var = tk.BooleanVar(value=True)
        self.env_seal_radius_var = tk.IntVar(value=2)
        # Запас вложенности (воксели): внешняя ткань принудительно содержит
        # внутреннюю+margin, чтобы после сглаживания череп не торчал сквозь кожу.
        self.env_nest_margin_var = tk.IntVar(value=1)
        # Параллельное меширование тканей (0 = авто ~половина ядер).
        self.env_jobs_var = tk.IntVar(value=0)
        # Децимация итоговых поверхностей (доля граней; 0.5 = 50%, 0 = выкл).
        self.env_decimate_var = tk.DoubleVar(value=0.0)

        # --- Постобработка поверхностей (surface_cleaner) ---
        self.surf_clean_var = tk.BooleanVar(value=True)
        self.surf_taubin_var = tk.IntVar(value=30)
        self.surf_minfaces_var = tk.IntVar(value=1000)
        # MMC (.node/.elem) — нужен только для тетраэдрального решателя MMC.
        # Для surface-MC (свой код) не нужен → по умолчанию выключен.
        self.export_mmc_var = tk.BooleanVar(value=False)

        # --- Состояние выполнения ---
        self._current_process: Optional[subprocess.Popen] = None
        self._stop_requested = threading.Event()
        self._log_queue: queue.Queue = queue.Queue()

        self.build_ui()
        self._drain_log_queue()

    # ─────────────────────────────────────────────────────────── UI ──

    def _make_scrollable(self, parent: tk.Widget) -> tk.Frame:
        """Вертикально прокручиваемая область. Возвращает внутренний фрейм,
        в который складываются секции формы."""
        outer = tk.Frame(parent)
        outer.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        # scrollregion следует за содержимым; ширина inner = ширине канвы (без гориз. скролла)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def _on_wheel(event: tk.Event) -> None:
            # не перехватываем колесо над консолью — она скроллится сама
            if hasattr(self, "log_area") and str(event.widget).startswith(str(self.log_area)):
                return
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", _on_wheel)
        self._canvas = canvas
        return inner

    def build_ui(self) -> None:
        # ── Фиксированный низ: кнопки запуска (всегда видимы) ──────────────
        f_btn = tk.Frame(self.root)
        f_btn.pack(side="bottom", fill="x", padx=10, pady=(4, 6))
        self.start_btn = tk.Button(
            f_btn,
            text="▶  ЗАПУСТИТЬ КОНВЕЙЕР",
            font=("Arial", 12, "bold"),
            bg="green",
            fg="white",
            command=self._on_start,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.stop_btn = tk.Button(
            f_btn,
            text="■  СТОП",
            font=("Arial", 12, "bold"),
            bg="red",
            fg="white",
            state="disabled",
            command=self._on_stop,
        )
        self.stop_btn.pack(side="left", padx=(5, 0), ipady=8)

        # ── Фиксированный низ: консоль (фикс. высота, всегда видима) ───────
        f_log = tk.LabelFrame(self.root, text="Консоль (Логи)", padx=10, pady=6)
        f_log.pack(side="bottom", fill="both", expand=False, padx=10, pady=(0, 4))
        self.log_area = scrolledtext.ScrolledText(
            f_log,
            bg="black",
            fg="lightgreen",
            font=("Consolas", 10),
            height=10,
        )
        self.log_area.pack(fill="both", expand=True)
        self.log_area.tag_configure("err", foreground="#ff6b6b")
        self.log_area.tag_configure("ok", foreground="#6bff6b")
        self.log_area.tag_configure("ts", foreground="#888888")

        # ── Прокручиваемая форма (верх, растягивается) ────────────────────
        content = self._make_scrollable(self.root)

        # 0. Файл конфигурации
        f_cfg = tk.LabelFrame(content, text="0. Файл конфигурации", padx=10, pady=8)
        f_cfg.pack(fill="x", padx=10, pady=5)
        f_cfg_row = tk.Frame(f_cfg)
        f_cfg_row.pack(fill="x", pady=2)
        tk.Label(f_cfg_row, text="Конфиг (.json):", width=22, anchor="w").pack(side="left")
        tk.Entry(f_cfg_row, textvariable=self.config_path_var).pack(
            side="left", fill="x", expand=True, padx=5
        )
        tk.Button(
            f_cfg_row, text="Обзор...", command=lambda: self._browse_file(self.config_path_var)
        ).pack(side="right")
        tk.Button(f_cfg_row, text="Загрузить", command=self._load_config, fg="darkblue").pack(
            side="right", padx=4
        )

        # 1. Входные данные
        f_files = tk.LabelFrame(content, text="1. Входные данные и пути", padx=10, pady=8)
        f_files.pack(fill="x", padx=10, pady=5)
        self._file_row(
            f_files, "Исходный объем (.npy/.img/.rawb):", self.input_vol_var, self._browse_file
        )
        self._file_row(f_files, "INFO.txt:", self.minc_info_var, self._browse_file)
        self._file_row(f_files, "Папка сохранения:", self.output_dir_var, self._browse_dir)

        # 2. Параметры предобработки
        f_set = tk.LabelFrame(content, text="2. Параметры предобработки", padx=10, pady=8)
        f_set.pack(fill="x", padx=10, pady=5)
        self._param_row(f_set, "Уровень среза Z (мм, пусто=выкл):", self.z_cut_var)
        self._param_row(f_set, "Обрезка по Y (индекс, пусто=выкл):", self.y_end_idx_var)
        self._param_row(f_set, "Отбраковка шума (мин. вокселей):", self.min_voxels_var)
        self._param_row(
            f_set, "Сглаживание меток σ (воксель, Этап 3):", self.label_smooth_sigma_var
        )

        f_workers = tk.Frame(f_set)
        f_workers.pack(fill="x", pady=2)
        tk.Label(
            f_workers, text=f"Потоков (1–{self.max_cores}):", width=35, anchor="w", fg="darkred"
        ).pack(side="left")
        tk.Spinbox(
            f_workers, from_=1, to=self.max_cores, textvariable=self.workers_var, width=10
        ).pack(side="left")

        # 3. Этапы предобработки
        f_pre = tk.LabelFrame(content, text="3. Этапы предобработки", padx=10, pady=8)
        f_pre.pack(fill="x", padx=10, pady=5)
        for var, step in zip(self.pre_step_vars, PRE_STEPS):
            tk.Checkbutton(f_pre, text=step[1], variable=var).pack(anchor="w")

        # 4. Режим генерации сеток
        f_mode = tk.LabelFrame(content, text="4. Режим генерации сеток", padx=10, pady=8)
        f_mode.pack(fill="x", padx=10, pady=5)

        rb_classic = tk.Radiobutton(
            f_mode,
            text="Классический: npy2vtk → meshValidator",
            variable=self.mesh_mode_var,
            value="classic",
            command=self._on_mode_change,
        )
        rb_classic.pack(anchor="w")

        rb_cgal = tk.Radiobutton(
            f_mode,
            text="Конформный CGAL (pygalmesh): единая тет-сетка + VTK + валидация",
            variable=self.mesh_mode_var,
            value="cgal",
            command=self._on_mode_change,
            fg="darkblue",
        )
        rb_cgal.pack(anchor="w", pady=(4, 0))

        rb_remesh = tk.Radiobutton(
            f_mode,
            text="CGAL-remesh (C++): образ → Mesh_3 → тет-ремешинг → гладкие поверхности + MMC",
            variable=self.mesh_mode_var,
            value="cgal-remesh",
            command=self._on_mode_change,
            fg="dark green",
        )
        rb_remesh.pack(anchor="w", pady=(4, 0))

        rb_env = tk.Radiobutton(
            f_mode,
            text="Оболочки (build_envelopes): вложенные внешние оболочки тканей",
            variable=self.mesh_mode_var,
            value="envelopes",
            command=self._on_mode_change,
            fg="#8000a0",
        )
        rb_env.pack(anchor="w", pady=(4, 0))

        # Описание классического режима
        self._classic_info = tk.Label(
            f_mode,
            text=("  Этап 3а: Децимация (целевых полигонов):                    "),
            fg="gray40",
            font=("Arial", 9),
        )
        self._classic_info.pack(anchor="w", pady=(4, 0))
        self._param_row(f_mode, "  Целевое число полигонов (decimate):", self.target_faces_var)
        tk.Checkbutton(
            f_mode,
            text="  Анти-алиасинг мешей перед генерацией (Smooth Marching Cubes)",
            variable=self.use_smooth_mc_var,
            fg="blue",
        ).pack(anchor="w", pady=2)
        tk.Checkbutton(
            f_mode,
            text="  Считать метрики Max/RMS Error [медленно, требует RAM]",
            variable=self.calc_metrics_var,
            fg="purple",
        ).pack(anchor="w", pady=2)

        # Параметры CGAL (pygalmesh)
        self._cgal_frame = tk.LabelFrame(f_mode, text="Параметры CGAL (pygalmesh)", padx=8, pady=6)
        self._cgal_frame.pack(fill="x", padx=20, pady=(6, 2))
        self._param_row(self._cgal_frame, "Max circumradius тет (мм):", self.cgal_max_radius_var)
        self._param_row(self._cgal_frame, "Max facet distance (мм):", self.cgal_facet_dist_var)
        self._param_row(
            self._cgal_frame, "Сглаживание Taubin (итераций, 0=выкл):", self.cgal_smooth_var
        )
        self._file_row(
            self._cgal_frame, "Python cgal_env:", self.cgal_python_var, self._browse_file
        )

        # Параметры CGAL-remesh (C++)
        self._remesh_frame = tk.LabelFrame(
            f_mode, text="Параметры CGAL-remesh (C++)", padx=8, pady=6
        )
        self._param_row(self._remesh_frame, "Facet size (мм):", self.rm_facet_size_var)
        self._param_row(self._remesh_frame, "Facet distance (мм):", self.rm_facet_dist_var)
        self._param_row(self._remesh_frame, "Cell size (мм):", self.rm_cell_size_var)
        tk.Checkbutton(
            self._remesh_frame,
            fg="dark green",
            text="Тетраэдральный ремешинг (выкл = ~14× быстрее; для surface-MC достаточно)",
            variable=self.rm_do_remesh_var,
        ).pack(anchor="w", pady=(2, 0))
        self._param_row(
            self._remesh_frame, "  Target edge (мм, при ремешинге):", self.rm_target_edge_var
        )
        self._param_row(self._remesh_frame, "  Итераций ремешинга:", self.rm_iterations_var)
        # Критерий manifold() в Mesh_3 на воксельном образе рефайнит до
        # исчерпания памяти (std::bad_alloc) и практически никогда не сходится —
        # поэтому здесь он НЕ используется. Манифолдность даёт Этап 3 (сглаживание
        # меток), а не этот флаг.
        tk.Label(
            self._remesh_frame,
            justify="left",
            fg="gray40",
            font=("Arial", 8),
            text=(
                "Манифолдность → через «Этап 3: Сглаживание меток» (σ).\n"
                "Флаг Mesh_3 manifold() отключён — он исчерпывает память."
            ),
        ).pack(anchor="w", pady=(2, 4))
        self._file_row(
            self._remesh_frame, "mesh_and_remesh.exe:", self.rm_exe_var, self._browse_file
        )
        self._file_row(
            self._remesh_frame, "MSYS2 ucrt64\\bin:", self.rm_msys2_bin_var, self._browse_dir
        )
        self._file_row(
            self._remesh_frame, "Python cgal_env:", self.cgal_python_var, self._browse_file
        )

        # Постобработка поверхностей: заливка (watertight) + сглаживание + наружные
        # нормали — нужна для surface-MC (перенос света по поверхностям).
        _surf = tk.LabelFrame(
            self._remesh_frame, text="Чистка поверхностей (для surface-MC)", padx=6, pady=4
        )
        _surf.pack(fill="x", pady=(6, 2))
        tk.Checkbutton(
            _surf,
            text="Чистить поверхности (pymeshfix + Taubin + наружные нормали)",
            variable=self.surf_clean_var,
            fg="dark green",
        ).pack(anchor="w")
        self._param_row(_surf, "  Taubin (итераций сглаживания):", self.surf_taubin_var)
        self._param_row(_surf, "  Отброс осколков (< N граней):", self.surf_minfaces_var)
        tk.Checkbutton(
            _surf,
            text="Экспорт MMC (.node/.elem) — только для тетр. решателя MMC",
            variable=self.export_mmc_var,
            fg="gray40",
        ).pack(anchor="w", pady=(4, 0))

        # --- Панель режима "envelopes" ---
        self._env_frame = tk.LabelFrame(
            f_mode, text="Параметры оболочек (build_envelopes)", padx=8, pady=6
        )
        self._param_row(self._env_frame, "Facet size (мм):", self.env_facet_var)
        self._param_row(self._env_frame, "Facet distance (мм):", self.env_dist_var)
        self._param_row(self._env_frame, "Taubin (сглаживание):", self.env_taubin_var)
        tk.Checkbutton(
            self._env_frame,
            fg="#8000a0",
            text="Запечатывать сквозные тоннели-арки (genus→0; кожа без дыр/выпираний)",
            variable=self.env_seal_var,
        ).pack(anchor="w", pady=(2, 0))
        self._param_row(
            self._env_frame,
            "  Радиус открытия (2 = закрыть ~0.4мм арки):",
            self.env_seal_radius_var,
        )
        self._param_row(
            self._env_frame,
            "Запас вложенности (воксели, ≥1 = череп не торчит):",
            self.env_nest_margin_var,
        )
        self._param_row(
            self._env_frame, "Параллельных задач (0 = авто, ~½ ядер):", self.env_jobs_var
        )
        self._param_row(
            self._env_frame,
            "Децимация итоговых мешей (0.5 = 50%, 0 = выкл):",
            self.env_decimate_var,
        )
        tk.Label(
            self._env_frame,
            justify="left",
            fg="gray40",
            font=("Arial", 8),
            text=(
                "Для каждой ткани: fill_holes(ткань ∪ вложенные) → CGAL → внешняя\n"
                "оболочка → чистка. Вложенность — из 'envelope_parents' в конфиге.\n"
                "facet≈0.10 — оптимум детализации; тоннели от facet почти не зависят.\n"
                "Нужны также exe/MSYS2/cgal_env (берутся из полей CGAL-remesh/конфига)."
            ),
        ).pack(anchor="w", pady=(2, 2))

        self._on_mode_change()  # применить начальное состояние

    def _on_mode_change(self) -> None:
        """Показывает/скрывает параметры в зависимости от выбранного режима."""
        mode = self.mesh_mode_var.get()
        self._cgal_frame.pack_forget()
        self._remesh_frame.pack_forget()
        self._env_frame.pack_forget()
        if mode == "cgal":
            self._cgal_frame.pack(fill="x", padx=20, pady=(6, 2))
        elif mode == "cgal-remesh":
            self._remesh_frame.pack(fill="x", padx=20, pady=(6, 2))
        elif mode == "envelopes":
            self._env_frame.pack(fill="x", padx=20, pady=(6, 2))

    # ─────────────────────────────────────────── Helpers ──

    def _file_row(self, parent: tk.Widget, label: str, var: tk.StringVar, cmd) -> None:
        f = tk.Frame(parent)
        f.pack(fill="x", pady=2)
        tk.Label(f, text=label, width=22, anchor="w").pack(side="left")
        tk.Entry(f, textvariable=var).pack(side="left", fill="x", expand=True, padx=5)
        tk.Button(f, text="Обзор...", command=lambda: cmd(var)).pack(side="right")

    def _param_row(self, parent: tk.Widget, label: str, var) -> None:
        f = tk.Frame(parent)
        f.pack(fill="x", pady=2)
        tk.Label(f, text=label, width=40, anchor="w").pack(side="left")
        tk.Entry(f, textvariable=var, width=12).pack(side="left")

    @staticmethod
    def _parse_optional(text: str, cast):
        """Пустая строка → None; иначе приводит к типу cast (может бросить ValueError)."""
        text = text.strip()
        return None if text == "" else cast(text)

    def _browse_file(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename()
        if path:
            var.set(path)

    def _browse_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _load_config(self) -> None:
        """Заполняет поля UI из выбранного файла конфигурации."""
        path = self.config_path_var.get().strip()
        if not path or not os.path.exists(path):
            self._log(f"Ошибка: конфиг не найден: {path}", "err")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"Ошибка чтения конфига: {exc}", "err")
            return

        def _opt_str(value) -> str:
            return "" if value is None else str(value)

        self.input_vol_var.set(cfg.get("input_volume", ""))
        self.minc_info_var.set(cfg.get("info_txt", ""))
        self.output_dir_var.set(cfg.get("output_dir", ""))
        self.z_cut_var.set(_opt_str(cfg.get("z_cut_mm")))
        self.y_end_idx_var.set(_opt_str(cfg.get("y_end_idx")))
        self.min_voxels_var.set(int(cfg.get("min_voxels", 10000)))
        self.target_faces_var.set(int(cfg.get("target_faces", 100000)))
        self.use_smooth_mc_var.set(bool(cfg.get("use_smooth_mc", True)))
        self.calc_metrics_var.set(bool(cfg.get("calc_metrics", False)))
        self.workers_var.set(int(cfg.get("workers", 1)))
        self.cgal_max_radius_var.set(float(cfg.get("cgal_max_radius", 3.0)))
        self.cgal_facet_dist_var.set(float(cfg.get("cgal_facet_dist", 0.5)))
        self.cgal_smooth_var.set(int(cfg.get("cgal_smooth_iterations", 20)))
        self.label_smooth_sigma_var.set(float(cfg.get("label_smooth_sigma", 2.0)))
        self.rm_facet_size_var.set(float(cfg.get("remesh_facet_size", 0.4)))
        self.rm_facet_dist_var.set(float(cfg.get("remesh_facet_dist", 0.15)))
        self.rm_cell_size_var.set(float(cfg.get("remesh_cell_size", 0.6)))
        self.rm_target_edge_var.set(float(cfg.get("remesh_target_edge", 0.4)))
        self.rm_iterations_var.set(int(cfg.get("remesh_iterations", 2)))
        self.rm_manifold_var.set(bool(cfg.get("remesh_manifold", False)))
        self.rm_do_remesh_var.set(bool(cfg.get("remesh_do_remesh", False)))
        if cfg.get("remesh_exe"):
            self.rm_exe_var.set(cfg["remesh_exe"])
        if cfg.get("msys2_bin"):
            self.rm_msys2_bin_var.set(cfg["msys2_bin"])
        self.surf_clean_var.set(bool(cfg.get("surface_clean", True)))
        self.surf_taubin_var.set(int(cfg.get("surface_taubin", 30)))
        self.surf_minfaces_var.set(int(cfg.get("surface_min_faces", 1000)))
        self.export_mmc_var.set(bool(cfg.get("export_mmc", False)))
        self.env_facet_var.set(float(cfg.get("envelope_facet_size", 0.10)))
        self.env_dist_var.set(float(cfg.get("envelope_facet_dist", 0.05)))
        self.env_taubin_var.set(int(cfg.get("envelope_taubin", 30)))
        self.env_seal_var.set(bool(cfg.get("envelope_seal_tunnels", True)))
        self.env_seal_radius_var.set(int(cfg.get("envelope_seal_radius", 2)))
        self.env_nest_margin_var.set(int(cfg.get("envelope_nest_margin", 1)))
        self.env_jobs_var.set(int(cfg.get("envelope_jobs", 0)))
        self.env_decimate_var.set(float(cfg.get("envelope_decimate", 0.0)))
        self._log(f"--- Конфиг загружен: {path} ---", "ok")

    # ──────────────────────────────────────── Log system ──

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _schedule_log(self, message: str, tag: str = "") -> None:
        self._log_queue.put((message, tag))

    def _drain_log_queue(self) -> None:
        try:
            while True:
                message, tag = self._log_queue.get_nowait()
                self.log_area.insert(tk.END, message + "\n", tag or None)
                self.log_area.see(tk.END)
                logger.debug(message)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_log_queue)

    def _log(self, message: str, tag: str = "") -> None:
        self._schedule_log(f"[{self._ts()}] {message}", tag)

    # ────────────────────────────────────── Pipeline ──

    def _on_start(self) -> None:
        in_vol = self.input_vol_var.get().strip()
        out_dir = self.output_dir_var.get().strip()
        if not in_vol or not out_dir:
            messagebox.showerror("Ошибка", "Выберите входной файл и папку вывода!")
            return

        info_txt = self._validate_info_txt()
        if info_txt is None:
            return

        # Если входной файл требует конвертации — пайплайн работает с .npy
        _CONV_EXTS = {".img", ".rawb"}
        input_for_pipeline = in_vol
        ext = os.path.splitext(in_vol)[1].lower()
        if ext in _CONV_EXTS:
            npy_path = os.path.splitext(in_vol)[0] + ".npy"
            input_for_pipeline = npy_path
            self._log(f"[INFO] Входной {ext} → будет конвертирован в {npy_path}", "ok")

        # Разбираем опциональные обрезки (пусто → null/отключено).
        try:
            z_cut = self._parse_optional(self.z_cut_var.get(), float)
            y_end = self._parse_optional(self.y_end_idx_var.get(), int)
        except ValueError as exc:
            messagebox.showerror("Ошибка", f"Некорректное значение обрезки: {exc}")
            return

        config_path = self.config_path_var.get().strip() or CONFIG_FILE

        # Читаем текущий конфиг, чтобы сохранить поля не управляемые UI.
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError):
            config = {}

        config.update(
            {
                "info_txt": info_txt,
                "input_volume": input_for_pipeline,
                "output_dir": out_dir,
                "z_cut_mm": z_cut,
                "y_end_idx": y_end,
                "min_voxels": self.min_voxels_var.get(),
                "target_faces": self.target_faces_var.get(),
                "use_smooth_mc": self.use_smooth_mc_var.get(),
                "calc_metrics": self.calc_metrics_var.get(),
                "workers": self.workers_var.get(),
                "cgal_max_radius": self.cgal_max_radius_var.get(),
                "cgal_facet_dist": self.cgal_facet_dist_var.get(),
                "cgal_smooth_iterations": self.cgal_smooth_var.get(),
                "label_smooth_sigma": self.label_smooth_sigma_var.get(),
                "remesh_facet_size": self.rm_facet_size_var.get(),
                "remesh_facet_dist": self.rm_facet_dist_var.get(),
                "remesh_cell_size": self.rm_cell_size_var.get(),
                "remesh_target_edge": self.rm_target_edge_var.get(),
                "remesh_iterations": self.rm_iterations_var.get(),
                "remesh_manifold": self.rm_manifold_var.get(),
                "remesh_do_remesh": self.rm_do_remesh_var.get(),
                "remesh_exe": self.rm_exe_var.get().strip(),
                "msys2_bin": self.rm_msys2_bin_var.get().strip(),
                "surface_clean": self.surf_clean_var.get(),
                "surface_taubin": self.surf_taubin_var.get(),
                "surface_min_faces": self.surf_minfaces_var.get(),
                "export_mmc": self.export_mmc_var.get(),
                "envelope_facet_size": self.env_facet_var.get(),
                "envelope_facet_dist": self.env_dist_var.get(),
                "envelope_taubin": self.env_taubin_var.get(),
                "envelope_seal_tunnels": self.env_seal_var.get(),
                "envelope_seal_radius": self.env_seal_radius_var.get(),
                "envelope_nest_margin": self.env_nest_margin_var.get(),
                "envelope_jobs": self.env_jobs_var.get(),
                "envelope_decimate": self.env_decimate_var.get(),
            }
        )
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        self._log(f"--- КОНФИГ СОХРАНЁН: {config_path} ---", "ok")
        self._stop_requested.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._run_pipeline, daemon=True).start()

    def _on_stop(self) -> None:
        self._stop_requested.set()
        proc = self._current_process
        if proc and proc.poll() is None:
            self._log("!! ЗАПРОШЕНА ОСТАНОВКА — завершаем процесс...", "err")
            try:
                proc.terminate()
            except OSError:
                pass
        self.stop_btn.config(state="disabled")

    def _validate_info_txt(self) -> Optional[str]:
        """Validate the selected INFO.txt and return its path, or None on error."""
        info_path = self.minc_info_var.get().strip()
        if not info_path:
            self._log("Ошибка: укажите путь к INFO.txt!", "err")
            return None
        if not os.path.exists(info_path):
            self._log(f"Ошибка: файл INFO.txt не найден: {info_path}", "err")
            return None
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from parse_info_txt import parse_info_txt as _parse_info

            _info = _parse_info(info_path)
            if _info._voxel_size is None:
                self._log(
                    "Предупреждение: 'Voxel size' не найден в INFO.txt — spacing будет 1.0мм", "err"
                )
            msg = (
                f"[INFO.txt] spacing=({_info.dx},{_info.dy},{_info.dz})мм  "
                f"z_start={_info.z_start_mm}  меток={len(_info.tissue_labels)}"
            )
            self._log(msg, "ok")
            return info_path
        except Exception as exc:
            self._log(f"Ошибка чтения INFO.txt: {exc}", "err")
            return None

    # ──────────────────────────────────── Script runner ──

    # Задача = (подпись, argv, env_extra, check_path)
    #   argv        — полная команда для subprocess (python+скрипт ИЛИ нативный .exe);
    #   env_extra   — переменные окружения поверх базового env (напр. PATH для .exe);
    #   check_path  — файл, который должен существовать перед запуском.
    def _build_task_list(self) -> list[tuple[str, list[str], dict, str]]:
        tasks: list[tuple[str, list[str], dict, str]] = []
        cfg_path = self.config_path_var.get().strip() or CONFIG_FILE
        cfg_arg = ["--config", cfg_path]
        py = sys.executable

        # Шаг 0: конвертация в .npy (если нужно)
        in_vol = self.input_vol_var.get().strip()
        _CONVERTERS = {".img": "img2npy.py", ".rawb": "rawb2npy.py"}
        ext = os.path.splitext(in_vol)[1].lower()
        if ext in _CONVERTERS:
            script = _CONVERTERS[ext]
            npy_path = os.path.splitext(in_vol)[0] + ".npy"
            info_txt_path = self.minc_info_var.get().strip()
            tasks.append((script, [py, script, in_vol, info_txt_path, npy_path], {}, script))

        # Предобработка (в т.ч. Этап 3: label_smoother — читает σ из конфига)
        for var, step in zip(self.pre_step_vars, PRE_STEPS):
            if var.get():
                script = step[0]
                tasks.append((script, [py, script] + cfg_arg, {}, script))

        # Режим генерации сеток
        mode = self.mesh_mode_var.get()
        if mode == "cgal-remesh":
            tasks += self._build_cgal_remesh_tasks(cfg_arg, py)
        elif mode == "envelopes":
            argv = (
                [py, "build_envelopes.py"]
                + cfg_arg
                + [
                    "--facet-size",
                    str(self.env_facet_var.get()),
                    "--facet-distance",
                    str(self.env_dist_var.get()),
                    "--taubin",
                    str(self.env_taubin_var.get()),
                    "--seal-open-radius",
                    str(self.env_seal_radius_var.get()),
                    "--nest-margin",
                    str(self.env_nest_margin_var.get()),
                    "--jobs",
                    str(self.env_jobs_var.get()),
                    "--decimate",
                    str(self.env_decimate_var.get()),
                ]
            )
            if not self.env_seal_var.get():
                argv.append("--no-seal-tunnels")
            cgal_py = self.cgal_python_var.get().strip()
            if cgal_py:
                argv += ["--cgal-python", cgal_py]
            # exe / MSYS2 bin: build_envelopes reads them from config, but honour
            # the GUI overrides if the user changed them in the CGAL-remesh panel.
            if self.rm_exe_var.get().strip():
                argv += ["--exe", self.rm_exe_var.get().strip()]
            if self.rm_msys2_bin_var.get().strip():
                argv += ["--msys2-bin", self.rm_msys2_bin_var.get().strip()]
            tasks.append(("build_envelopes.py", argv, {}, "build_envelopes.py"))
        else:
            for script in MESH_MODES.get(mode, []):
                argv = [py, script] + cfg_arg
                if script == _CGAL_SCRIPT:
                    argv += [
                        "--cgal",
                        "--max-radius",
                        str(self.cgal_max_radius_var.get()),
                        "--facet-dist",
                        str(self.cgal_facet_dist_var.get()),
                    ]
                    cgal_py = self.cgal_python_var.get().strip()
                    if cgal_py and os.path.isfile(cgal_py):
                        argv[0] = cgal_py
                    else:
                        self._log(
                            f"[WARN] CGAL Python не найден: '{cgal_py}' — "
                            "используем sys.executable (pygalmesh может отсутствовать)",
                            "err",
                        )
                tasks.append((script, argv, {}, script))

        return tasks

    def _build_cgal_remesh_tasks(self, cfg_arg: list[str], py: str) -> list[tuple]:
        """Режим C++: npy→inr (cgal_env) → mesh_and_remesh.exe (нативно) → валидация."""
        out_dir = self.output_dir_var.get().strip()
        conformal = os.path.join(out_dir, "vtk_export", "conformal")
        inr = os.path.join(conformal, "volume.inr")
        mesh = os.path.join(conformal, "brain_full_conformal.mesh")
        npy2inr = os.path.join("cgal_remesh", "npy2inr.py")
        exe = self.rm_exe_var.get().strip()
        msys_bin = self.rm_msys2_bin_var.get().strip()

        # 1) .npy → .inr  (pure NumPy native writer — main env, no pygalmesh)
        t1 = ("npy2inr.py", [py, npy2inr] + cfg_arg + ["--out", inr], {}, npy2inr)

        # 2) mesh_and_remesh.exe — нативный, нужны DLL из MSYS2 ucrt64\bin на PATH
        exe_args = [
            exe,
            inr,
            mesh,
            "--facet-size",
            str(self.rm_facet_size_var.get()),
            "--facet-distance",
            str(self.rm_facet_dist_var.get()),
            "--cell-size",
            str(self.rm_cell_size_var.get()),
        ]
        if self.rm_do_remesh_var.get():
            exe_args += [
                "--target-edge-length",
                str(self.rm_target_edge_var.get()),
                "--iterations",
                str(self.rm_iterations_var.get()),
            ]
        else:
            exe_args.append("--no-remesh")  # Mesh_3 only — ~14× быстрее, для surface-MC
        # NB: --manifold намеренно НЕ пробрасывается — критерий manifold() в
        # Mesh_3 исчерпывает память на воксельных образах. Манифолдность даётся
        # предобработкой (Этап 3: label_smoother).
        env_extra: dict = {}
        if msys_bin and os.path.isdir(msys_bin):
            env_extra["PATH"] = msys_bin + os.pathsep + os.environ.get("PATH", "")
        else:
            self._log(f"[WARN] MSYS2 bin не найден: '{msys_bin}' — DLL могут не загрузиться", "err")
        t2 = ("mesh_and_remesh.exe", exe_args, env_extra, exe)

        # 3) Валидация + экспорт поверхностей (в явную папку) + таблица оптики.
        #    Для surface-MC нужны поверхности и таблица label→(n,μa,μs,g).
        #    MMC (.node/.elem) — только для тетраэдрального решателя MMC (опц.).
        surfaces_dir = os.path.join(conformal, "surfaces")
        mc_args = cfg_arg + ["--export-surfaces", surfaces_dir, "--props"]
        if self.export_mmc_var.get():
            mc_args.append("--export-mmc")
        t3 = ("mc_mesh_check.py", [py, "mc_mesh_check.py"] + mc_args, {}, "mc_mesh_check.py")
        tasks = [t1, t2, t3]

        # 4) Чистка поверхностей для surface-MC: заливка (watertight) + сглаживание
        #    + единые наружные нормали. Правит те же файлы в surfaces_dir на месте.
        if self.surf_clean_var.get():
            t4 = (
                "surface_cleaner.py",
                [
                    py,
                    "surface_cleaner.py",
                    "--dir",
                    surfaces_dir,
                    "--taubin",
                    str(self.surf_taubin_var.get()),
                    "--min-faces",
                    str(self.surf_minfaces_var.get()),
                ],
                {},
                "surface_cleaner.py",
            )
            tasks.append(t4)
        return tasks

    _MODE_LABELS = {
        "classic": "КЛАССИЧЕСКИЙ",
        "cgal": "CGAL (pygalmesh)",
        "cgal-remesh": "CGAL-remesh (C++)",
        "envelopes": "Оболочки (build_envelopes)",
    }

    def _run_pipeline(self) -> None:
        tasks = self._build_task_list()
        base_env = os.environ.copy()
        base_env["PYTHONIOENCODING"] = "utf-8"
        base_env["MPLBACKEND"] = "Agg"
        base_env.setdefault("VTK_SILENCE_GET_VOID_POINTER_WARNINGS", "1")

        mode = self.mesh_mode_var.get()
        self._log(f"\n>> РЕЖИМ: {self._MODE_LABELS.get(mode, mode)} | Шагов: {len(tasks)}", "ok")

        ok = True
        for label, argv, env_extra, check in tasks:
            if self._stop_requested.is_set():
                self._log("Конвейер остановлен пользователем.", "err")
                ok = False
                break
            env = dict(base_env)
            env.update(env_extra)
            if not self._run_task(label, argv, env, check):
                ok = False
                break

        tag = "ok" if ok else "err"
        self._log(f"\n>> КОНВЕЙЕР {'УСПЕШНО ЗАВЕРШЁН' if ok else 'ЗАВЕРШЁН С ОШИБКОЙ'}.", tag)
        self.root.after(0, self._reset_buttons)

    def _reset_buttons(self) -> None:
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def _run_task(
        self,
        label: str,
        argv: list[str],
        env: dict,
        check_path: str,
    ) -> bool:
        self._log(f"\n{'='*52}", "ts")
        self._log(f">> {label}  {' '.join(argv[1:])}".strip(), "ok")
        self._log(f"   exec: {argv[0]}", "ts")

        if check_path and not os.path.exists(check_path):
            self._log(f"!! Файл '{check_path}' не найден!", "err")
            return False

        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except OSError as exc:
            self._log(f"!! Не удалось запустить: {exc}", "err")
            return False

        self._current_process = process

        def _reader(pipe, tag: str) -> None:
            try:
                for line in pipe:
                    stripped = line.rstrip()
                    if stripped:
                        self._log(stripped, tag)
            except ValueError:
                pass
            finally:
                pipe.close()

        t_out = threading.Thread(target=_reader, args=(process.stdout, ""), daemon=True)
        t_err = threading.Thread(target=_reader, args=(process.stderr, "err"), daemon=True)
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        process.wait()

        rc = process.returncode
        if rc == 0:
            self._log(f">> {label}: exit 0 — OK", "ok")
        else:
            self._log(f"!! {label}: exit {rc} — ОШИБКА", "err")

        self._current_process = None
        return rc == 0


if __name__ == "__main__":
    root = tk.Tk()
    app = PipelineApp(root)
    root.mainloop()
