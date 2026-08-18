# mouse-mesh-pipeline

Пайплайн превращает **размеченный воксельный атлас тканей** (`.npy`) в чистые
водонепроницаемые **поверхности по тканям** (и при желании конформную
тетраэдральную сетку), пригодные для **Monte-Carlo переноса света по
поверхностям**. Сделан под атлас мыши (кожа / череп / структуры мозга), но
обобщается на любой размеченный объём.

Главный результат — набор **вложенных внешних оболочек** тканей: именно такое
представление корректно для целевого MC-солвера (см. раздел
"Почему вложенные оболочки").

## Пайплайн вкратце

```
INFO.txt + atlas.npy
  parse_info_txt.py         спейсинг, имена меток, кроп
  -> connectivity_searcher.py   Этап 1: связные компоненты / слияния меток
  -> small_area_closer.py       Этап 2: удаление мелких областей
  -> label_smoother.py          Этап 3 (опц.): воксельное сглаживание меток
  -> 02_merged.npy
  -> режим сеток:
       classic      npy2vtk.py -> meshValidator.py    (независимые изоповерхности)
       cgal         npy2conformal_mesh.py             (pygalmesh, единая тет-сетка)
       cgal-remesh  npy2inr.py -> mesh_and_remesh.exe -> mc_mesh_check.py
       envelopes    build_envelopes.py                (рекомендуется для surface-MC)
  -> surface_cleaner.py     заливка watertight + Taubin + нормали наружу
  -> surfaces/surface_NN_<ткань>.vtk + optical_properties.csv
```

`pipeline_manager.py` — GUI на PySide6 (Qt): панели зависят от режима, живой лог,
вкладка метрик, тёмная/светлая тема, поле выбора python-интерпретатора; связывает
все этапы (предобработка, режим сеток, чистка). Требует `PySide6`. Старый Tkinter-
интерфейс сохранён как `pipeline_manager_tk.py` (запасной, без зависимостей).

## Структура репозитория

| путь | что |
|---|---|
| `*.py` (корень) | скрипты пайплайна (плоско, т.к. импортируют друг друга по имени) |
| `build_envelopes.py` | сборщик вложенных оболочек (рекомендуемый путь для surface-MC) |
| `mc_mesh_check.py` | проверка пригодности сеток для MC + экспорт поверхностей / MMC |
| `surface_cleaner.py` | заливка watertight + Taubin + нормали наружу + децимация |
| `pipeline_manager.py` | GUI на PySide6 (Qt), оркестрирующий все режимы; `pipeline_manager_tk.py` — старый Tkinter |
| `cgal_remesh/` | C++ CGAL-инструменты (`mesh_and_remesh.cpp`, `tet_remesh.cpp`) + `npy2inr.py` + сборка |
| `bin/win64/` | готовые Windows-бинарники C++ + их runtime-DLL |
| `installer/` | установщики под все ОС: Windows exe, Linux deb/rpm/AppImage/Arch, macOS pkg |
| `scripts/` | развёртывание conda-окружений + `requirements-*.txt` |
| `configs/` | примеры `pipeline_config*.json` (пути машинно-зависимы, правьте под себя) |
| `docs/` | отчёт аудита; сюда же Doxygen кладёт `docs/html/` |
| `.github/workflows/ci.yml` | CI: линт, доки, сборка C++ и установщика, релиз |

## Установка (готовые пакеты)

CI собирает установщики под все ОС; берите их из ассетов релиза (вкладка
Releases) или из артефактов конкретного прогона Actions. Философия одна: тонкий
установщик заводит **отдельный venv** и ставит зависимости через pip туда
(системный Python не трогается), плюс кладёт готовый нативный `mesh_and_remesh`.
Исключение — AppImage: он полностью самодостаточный (Python и все зависимости
внутри, ставить ничего не надо).

**Windows** — `mouse-mesh-pipeline-setup.exe`. На машине с Python 3.10+ копирует
пайплайн, создаёт venv, ставит зависимости, кладёт `mesh_and_remesh.exe` (+ DLL,
MSYS2 не нужен) и делает ярлык на рабочем столе.

**Linux (deb/rpm/Arch)** — нативный пакет с готовым бинарником CGAL; venv
создаётся в `/opt/mouse-mesh-pipeline/.venv` на этапе post-install:

```sh
sudo apt install ./mouse-mesh-pipeline_<ver>_amd64.deb            # Debian/Ubuntu
sudo dnf install ./mouse-mesh-pipeline-<ver>-1.x86_64.rpm         # Fedora/RHEL
sudo pacman -U  ./mouse-mesh-pipeline-<ver>-1-x86_64.pkg.tar.zst  # Arch
```

Запуск — команда `mouse-mesh-pipeline` или ярлык «Mouse Mesh Pipeline».

**Linux (AppImage)** — один самодостаточный файл, ставить ничего не нужно:

```sh
chmod +x mouse-mesh-pipeline-<ver>-x86_64.AppImage
./mouse-mesh-pipeline-<ver>-x86_64.AppImage
```

**macOS** — `mouse-mesh-pipeline-<ver>.pkg` (двойной клик). Ставит пайплайн в
`/usr/local/mouse-mesh-pipeline`, создаёт venv и кладёт `.app` в «Программы».
Нужен Python 3.10+ (python.org или brew). Пакет не подписан — при первом запуске
откройте через правый клик → «Открыть».

Оболочечному пайплайну не нужны conda/pygalmesh — `npy2inr` пишет `.inr` на чистом
NumPy.

## Установка из исходников

Два conda-окружения (научная база numpy/scipy + pip-дополнения). `cgal_env` нужен
только для старого pygalmesh-режима `cgal`, но НЕ для оболочек:

```powershell
# main-окружение (пайплайн + GUI): numpy, scipy, meshio, trimesh, pymeshlab, pymeshfix, vtk
scripts\setup_main_env.ps1        # или setup_main_env.sh
# cgal-окружение (только для pygalmesh)
scripts\setup_cgal_env.ps1
```

C++-инструменты (нужны MSYS2 UCRT64 g++ + gmp/mpfr + **заголовки CGAL 6.0.1**):

```powershell
cd cgal_remesh
powershell -ExecutionPolicy Bypass -File build.ps1          # последовательно
powershell -ExecutionPolicy Bypass -File build.ps1 -Tbb     # параллельный Mesh_3 (нужен Intel TBB)
```

Пакет MSYS2 CGAL 5.5.2 использовать **нельзя** (его тетраэдральный ремешинг сломан);
как скачать заголовки 6.0.1 и собрать oneTBB из исходников — в шапке
`cgal_remesh/build.ps1`.

## Использование: вложенные оболочки (рекомендуется)

```powershell
python build_envelopes.py --config configs\pipeline_config_mouse.json ^
       --facet-size 0.10 --facet-distance 0.05 --taubin 40 ^
       --nest-margin 2 --seal-open-radius 2 --decimate 0.5 --jobs 1
```

Результат: `surfaces_envelopes/surface_NN_<ткань>.vtk` + `optical_properties.csv`.
Вложенность берётся из ключа конфига `envelope_parents` (для мыши
`{"2":1,"4":2,"5":2,"6":2,"7":2}` — кожа содержит череп, череп содержит мозг).

Основные параметры:

- `--facet-size` — детализация поверхности (оптимум около размера вокселя; мельче
  только вылезает лестница).
- `--seal-tunnels` / `--seal-open-radius` — запечатывание сквозных тоннелей-арок
  (кожа становится genus 0).
- `--nest-margin` — на сколько вокселей внешняя ткань обязана выступать за
  внутренние, чтобы после сглаживания поверхности не пересекались (череп не
  торчит сквозь кожу).
- `--crop-recess` — утопить внутренние ткани от плоскости кропа, чтобы торец
  закрывала только кожа (авто = nest-margin).
- `--decimate` — квадрик-децимация итоговых поверхностей до этой доли граней
  (`0.5` = 50%; watertight и вложенность сохраняются).
- `--jobs` — параллельное меширование тканей (с TBB-exe используйте `--jobs 1`).

Либо всё из GUI: `python pipeline_manager.py`, режим "Оболочки".

## Почему вложенные оболочки

Целевой Monte-Carlo (`photonMove.cpp` / `mcml_intersection.cpp`) вызывает
`FindIntersectionLayer(surfaceId, layerId)` **без позиции и без нормали**, значит
слой по другую сторону поверхности обязан однозначно определяться парой
*(поверхность, слой)*. Это возможно только при **строгой вложенности**, где каждая
ткань представлена своей **внешней оболочкой** (air содержит кожу, кожа содержит
череп, череп содержит мозг); `layerId == 0` — среда/выход. Полная граница ткани
(с внутренними стенками вокруг вложенных органов) это ломает: поверхность кожи
снаружи граничит с air, а изнутри с черепом, и граница кожа|череп задаётся дважды.
`build_envelopes.py` строит именно такие вложенные оболочки:
`fill_holes(ткань + вложенные потомки)` -> CGAL Mesh_3 -> чистка.

## Производительность

Замер на мышином наборе (8 ядер, facet 0.10), полная сборка 6 тканей:

| конфиг | время | к базе |
|---|---|---|
| sequential exe, `--jobs 1` | 194 с | 1.0x |
| sequential exe, `--jobs 4` | 141 с | 1.4x |
| **TBB exe, `--jobs 1`** | **91 с** | **2.1x** |

- **GPU:** неприменим — у CGAL Mesh_3 нет GPU-бэкенда; воксельная морфология
  (scipy) и так занимает секунды.
- **Многопоток по тканям:** `build_envelopes.py --jobs` / `surface_cleaner.py --jobs`
  считают независимые ткани параллельно (потолок задаёт самая большая — кожа),
  примерно 1.4x.
- **Многопоток внутри одной сетки (главный рычаг):** `build.ps1 -Tbb` собирает
  параллельный CGAL Mesh_3 (`Parallel_tag`). Одна только кожа: **85 с -> 14 с
  (примерно 6x)**. Нужен Intel oneTBB, собранный тем же gcc (пакет MSYS2 конфликтует
  с закреплённым gcc 13 — соберите oneTBB v2022.0.0 из исходников, см. шапку
  `cgal_remesh/build.ps1`). С TBB-exe используйте `--jobs 1` (каждая сетка уже
  занимает все ядра; `--jobs > 1` даёт переподписку).

## Документация (Doxygen)

```
doxygen Doxyfile        # -> docs/html/index.html  (C++ и Python)
```

CI собирает доки на каждый push и выкладывает артефактом `doxygen-html`; на GitHub
дополнительно деплоит на GitHub Pages.

## CI и релизы

`.github/workflows/ci.yml` — задачи:

- **python** — ruff (реальные ошибки), проверка формата `black` и `clang-format`,
  байт-компиляция всех модулей.
- **docs** — сборка Doxygen HTML, артефакт `doxygen-html`.
- **cpp-linux / cpp-windows / cpp-macos** — сборка нативных бинарников CGAL (+TBB)
  под каждую ОС; артефакты `linux64/win64/macos-binaries`. На Windows это MSYS2
  UCRT64 g++, на macOS — clang + Homebrew CGAL с `dylibbundler`.
- **installer** — Windows `mouse-mesh-pipeline-setup.exe` (PyInstaller).
- **linux-packages** — `.deb` + `.rpm` + Arch `.pkg.tar.zst` из одного `nfpm.yaml`.
- **appimage** — самодостаточный `.AppImage` (внутри Miniforge-Python + зависимости).
- **macos-installer** — `.pkg` (`pkgbuild`).
- **pages** — деплой Doxygen на GitHub Pages (только GitHub, default-ветка).
- **release** — на теге `v*` публикует GitHub Release со всеми установщиками
  (exe / deb / rpm / pkg.tar.zst / AppImage / pkg), архивом исходников и `README.md`.

Формат кода закреплён `black` (`pyproject.toml`, длина строки 100) и `clang-format`
(`.clang-format`). Синтаксис — стандартный GitHub Actions; на **Gitea Actions**
идёт через `act_runner` (GitHub-only задачи `pages`/`release` там пропускаются).

Чтобы выпустить релиз с ассетами:

```powershell
git tag v1.0.0
git push origin v1.0.0     # и/или: git push github v1.0.0
```

## Лицензия

MIT — см. [LICENSE](LICENSE).
