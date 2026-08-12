# Аудит пайплайна: воксельные атласы → сетки для Monte-Carlo light transport

Дата: 2026-07-06.

Каждое утверждение о геометрии/алгоритме подкреплено **запуском** `mc_mesh_check.py` на
реальных выходных `.mesh` (мозг CRISP `subject04`, атлас мыши `atlas_380x992x208`) либо
чтением кода — не догадкой. Числа в отчёте воспроизведены в ходе этого аудита.

> Провенанс изменений: пять правок ниже были разработаны на ветке
> `audit-mc-mesh-refactor` и в этом аудите **независимо перепроверены** по коду и на
> реальных данных, затем перенесены (cherry-pick) на текущую ветку атомарными коммитами.
> Ключевой фикс инверсий тетов проверен эмпирически (см. §3).

---

## 1. Карта репозитория

### Активный пайплайн (проверено чтением + запуском)

| Файл | Роль | Окружение |
|------|------|-----------|
| `parse_info_txt.py` | Универсальный парсер `INFO.txt` (spacing, метки, слияния) | оба |
| `img2npy.py`, `rawb2npy.py` | Конвертеры Analyze `.img` / raw-bytes `.rawb` → `.npy` `(nZ,nY,nX)` | main |
| `connectivity_searcher.py` | Этап 1: слияния меток + связные компоненты → `01_labeled.npy` | main |
| `small_area_closer.py` | Этап 2: удаление мелких областей + восстановление меток → `02_merged.npy` | main |
| `npy2conformal_mesh.py` | Сетки: Tier A (VTK изоповерхности) / Tier B (`--cgal`, тет-сетка) | main / cgal_env |
| `npy2vtk.py` + `meshValidator.py` | Классический путь: marching cubes + CSG-разрешение пересечений | main |
| `pipeline_manager.py` | GUI-оркестратор (`--config`), запускает шаги в двух окружениях | main |
| `mc_mesh_check.py` | **(новый)** проверка пригодности сеток для MC + экспорт в MMC | оба |
| `pipeline_config.json`, `pipeline_config_mouse.json` | Конфиги датасетов | — |
| `scripts/` | Развёртывание двух conda-окружений (main + `cgal_env`) | — |

**Согласованность осей проверена на данных.** `.npy` имеет форму `(nZ, nY, nX)`
(подтверждено: `atlas` = `(208, 992, 380)`, `INFO.txt` Matrix size = `nX×nY×nZ` = `380×992×208`).
Spacing по осям — `(dz, dy, dx)`. Соответствие «оси ↔ spacing» согласовано во всех активных
скриптах: converters (`reshape(nZ,nY,nX)`), VTK `SetSpacing(dx,dy,dz)`, pygalmesh
`generate_from_array(vol, [dz,dy,dx])`.

### Вспомогательные (отчёт, бенчмарки, визуализация — не трогать)
`render_final_vtk.py`, `render_mesh_comparison.py`, `render_sphere.py`, `render_benchmark.py`,
`liibrary_benchmark.py`, `hole_filling_demo.py`, `npy_visualizer.py`, `ViewSurfases.py`,
`generate_test_data.py`, `npy_generator.py`, `compare2Volumes.py`, `compareVolumes.py`,
`compareMat.py`, `view_vol.py`, `report/` (LaTeX-отчёт).

### Legacy / мёртвый код (не удалять — только пометить; кандидаты на архив)
Одноразовые скрипты старых прогонов и не подключённые утилиты:
`doMousePolygon.py`, `doMouseSurfaceFile.py`, `doMouseTopSurfaceFile.py`, `doMouseVTK.py`,
`doTopMouseVTK.py`, `doTopBrainSurfaceFromHdr.py`, `doHistAndImage.py`, `doPhotosensitizer.py`,
`showLayer.py`, `showLayers.py`, `showMasks.py`, `resultsToVTK.py`, `resultToLOG10.py`,
`fromSurfaceToVTK.py`, `fromTsurfaceToSurface.py`, `layerToText.py`, `readSimulatedBrainLabels.py`,
`surfaceTriangleSize.py`, `calcTrianglesNumber.py`, `createDiamond.py`, `ct_prepare_script.py`,
`depth_mcml_vis.py`, `dicom.py`, `dicom_dir.py`, `run_exe.py`, `utils.py`, вся папка `meshes/`
(`collision.py`, `sat_collision.py`, `meshCollision.py`, `makeMesh.py`, `saveMesh.py`,
`vtkvis*.py`, `converter.py`, `read_seg_data.py`, …), плюс артефакты `prac_report (2).pdf`.

---

## 2. Баги и запахи (severity)

Severity: **BLOCKER** (ломает MC-результат), **HIGH** (падение/некорректность), **MED**,
**LOW/S** (запах). ✅ = исправлено на этой ветке, ⚠️ = рекомендация (§5).

| # | Severity | Файл | Проблема | Статус |
|---|----------|------|----------|--------|
| B1 | HIGH | `npy2conformal_mesh.py` | Tier A: `del … smoother …` даёт `NameError`, если сглаживание выключено (`--smooth 0`) — `smoother` объявлен только внутри `if`. | ✅ |
| B2 | **BLOCKER** | `npy2conformal_mesh.py` | Taubin-сглаживание тет-сетки выворачивает тетраэдры. **Воспроизведено в этом аудите**: чистая мышь 38 873 тетов, 20 итераций старого сглаживания → **222 инвертированных**; на поставленной сетке мыши — **286**. Отрицательный якобиан = неопределённое «внутри/снаружи» = блокер трассировки фотонов. | ✅ |
| B3 | MED | `small_area_closer.py` | Мажоритарное восстановление голосует по **несмёрженному** `original_volume` → слитая/занулённая метка может вернуться в итог. Латентно на текущей мыши (cerebrum проголосовал за 7), но проявляется, когда неканоническая метка доминирует в компоненте. | ✅ |
| B4 | HIGH | `connectivity_searcher.py`, `npy2conformal_mesh.py` | `UnicodeEncodeError` при выводе `→`/`─`/`↳` в лог на не-UTF-8 кодовой странице Windows (cp1251/cp1252) при прямом запуске (не из GUI, где задан `PYTHONIOENCODING`). | ✅ |
| B5 | LOW | `connectivity_searcher.py` | Лог обрезки по Y умножал Y-индекс на `dz` вместо `dy` (неверный мм-экстент при анизотропии; на мыши spacing изотропен → косметика). | ✅ |
| B6 | **BLOCKER** (surface-MC) | `npy2conformal_mesh.py` `validate_conformal_surfaces` | Децимация до фикс. `target_faces` + `pymeshfix(joincomp=True)` разрушают геометрию. **Замерено на мозге**: Fat **MaxErr 98.4 мм**, Muscle **142.8 мм**, Dura **132.6 мм**, Vessels **80.8 мм** (≈ поперечник bbox 142 мм — `joincomp` строит «мост» через всю голову между фрагментами ткани). Мышь: скелет **16 мм**. | ⚠️ R1 |
| B7 | MED | `npy2conformal_mesh.py` `validate_conformal_surfaces` | Вердикт «PASSED / ВСЕ СЕТКИ OK» опирается **только** на watertight, игнорируя genus и MaxErr → сетки со 143 мм ошибки помечены OK. | ⚠️ R2 (частично закрыто `mc_mesh_check.py`) |
| B8 | MED (операционный) | выходные `.mesh` | **Поставленная сетка мыши устарела и не соответствует текущему пайплайну.** На диске материалы `{1,2,3,4,5,6,7,8}` (`eye`, `label_8`), а текущий `02_merged.npy` = `{1,2,4,5,6,7,11,12}`. Плюс 286 инверсий (создана до фикса B2). **Нужна перегенерация.** | ⚠️ R0 |
| S1 | S | `npy2conformal_mesh.py` | `if was_decimated or True:` — мёртвая ветка (всегда истина). | ⚠️ R6 |
| S2 | S | `connectivity_searcher.py` | `recode_labels`, `_RECODE_MAP`, `LABEL_NAMES` не используются в `__main__`. | ⚠️ R6 |
| S3 | MED | `meshValidator.py` | `HIERARCHY = {3:100, 6:10}` захардкожен под метки CRISP (мозг/кожа); для мыши id иные → приоритеты CSG неверны. | ⚠️ R6 |
| S4 | MED | конфиги | `pipeline_config.json` (по README — «brain») указывает на данные **мыши**; CRISP `INFO.txt` в формате MINC, который `parse_info_txt` не разбирает → путь мозга держится на config-fallback. | ⚠️ R4 |
| S5 | MED | `small_area_closer.py` | `min_voxels=10000` удаляет мелкие ткани целиком. На мыши исчезают **eye (3)** и **adrenal glands (20)** — теряют собственные оптические свойства в MC. | ⚠️ R5 |
| S6 | LOW | `parse_info_txt.py` | Если одно имя класса встречается на нескольких строках с немонотонными canonical-метками (новая меньше существующей), в `label_names` появляются два ключа с одним именем, а старые merges не переуказываются. На текущих INFO.txt не проявляется. | ⚠️ R6 |

---

## 3. Проверка реализаций алгоритмов

- **Connected components / relabel** (`relabel_existing_labels`, connectivity=3 = 26-связность):
  корректно. Маски по меткам не пересекаются, `relabeled += labeled_mask` без двойной записи,
  нумерация сквозная (offset `next_label-1` на компоненту). ✅
- **Слияния меток из `INFO.txt`**: `canonical = min`, фоновые `{N:0}` реально зануляются.
  Проверено на реальном INFO мыши → `label_merges = {8:7, 10:0}`, `tissue_labels` без 8 и 10;
  компаундные строки с `+` («whole brain») пропускаются; `02_merged` не содержит 8 и 10. ✅
- **merge_small_regions / восстановление**: DT-присвоение мелких компонент ближайшей крупной
  корректно (фон 0 сохраняется). Восстановление тканей — корректно **после фикса B3**
  (голос по каноническим меткам). Побочный эффект S5 (потеря мелких тканей) — by design.
- **Taubin `_smooth_tet_points`**: λ=0.5, μ=−0.53, |μ|>λ (анти-усадка) — формула верна;
  `L@p − p` = зонтичный (umbrella) лапласиан; общий граф вершин ⇒ **общие граничные вершины
  двигаются вместе ⇒ конформность сохраняется**. Инверсии тетов — устранены guard'ом.
  **Эмпирическая проверка (этот аудит), чистая мышь, 20 итераций:**
  старое сглаживание → **222** инвертированных тета; guard'ированное → **0** (заморожено 770
  вершин, макс. смещение остальных 0.75 мм — сглаживание сохранено). ✅
- **Извлечение поверхностей из тетов** (`_extract_region_surface`): граничная грань = грань
  региона, встречающаяся ровно 1 раз (векторизовано через lex-sort). Топологически замкнуто;
  общие граничные вершины между регионами сохраняются в тет-сетке (конформный интерфейс).
  Ориентация из сырого winding чинится `fix_normals`. ✅
  ⚠️ Важно: **пер-тканевые VTK после `validate_conformal_surfaces` теряют взаимную
  конформность** — pymeshfix/децимация обрабатывают каждую поверхность отдельно, общие
  вершины расходятся. Для MMC это неважно (используется `.mesh`); для surface-MC — да.
- **Децимация + pymeshfix**: искажает геометрию (B6). `pymeshfix.repair(joincomp=True)` на
  фрагментированной ткани соединяет далёкие компоненты «мостом» длиной до bbox → MaxErr ≈ 143 мм.

---

## 4. Пригодность сеток для Monte-Carlo light transport

Целевой решатель — **MMC** (тетраэдральный). Он потребляет **объёмную конформную тет-сетку**
(`brain_full_conformal.mesh` → `.node/.elem`), а **не** отдельные децимированные поверхности.
Поэтому вердикт по тет-сетке — решающий; поверхности вторичны (нужны для surface-MC типа
MCX-mesh, где сейчас непригодны из-за B6). Проверка — `mc_mesh_check.py` на реальных `.mesh`.

### Тет-сетка — вердикт по критериям (воспроизведено запуском)

| Критерий | Мозг CRISP (`save_data_full_universal`) | Мышь (`mouse/save_data`, поставленная) |
|----------|------------------------------------------|-----------------------------------------|
| Положительный якобиан | ✅ **0 / 2 670 156** инвертированных | ❌ **286 / 38 873** (Taubin) — чинится B2 + перегенерацией |
| Конформность (грань ≤ 2 тетов) | ✅ 0 нарушений; 40 940 граничных граней | ✅ 0 нарушений; 5 684 граничных граней |
| Герметичность региона (нет дыр) | ✅ все регионы замкнуты | ✅ все регионы замкнуты |
| Многообразность границ | ⚠️ non-manifold рёбра на тройных стыках тканей — **норма** для мультиматериала | ⚠️ то же (skin 560, skeleton 418, olfactory 15) |
| Качество (слайверы q<0.01) | ✅ 0 (q_min 0.0102) | ⚠️ 5 слайверов (q_min 0.0010) |
| Единицы / масштаб | ✅ мм, bbox 142×216×180 | ✅ мм, bbox 16×30×28 |
| Разметка материалов | ✅ 10 регионов, region→ткань | ⚠️ 8 регионов, но **устаревшие** (B8) |

**Вердикт по тет-сеткам:**
- **Мозг CRISP — MC-READY** (после экспорта в `.node/.elem`). Конформна, без инверсий,
  герметична, без слайверов. Non-manifold рёбра на стыках трёх тканей — нормальная топология
  мультиматериальной CGAL-сетки, **не** блокер для объёмного MMC.
- **Мышь — БЛОКЕР до перегенерации.** Поставленный `.mesh` (а) устарел (B8: материалы не
  соответствуют текущему `02_merged`), (б) содержит 286 инверсий (создан до фикса B2).
  Требуется перегенерация Tier B с guard'ированным `_smooth_tet_points` (фикс уже в коде)
  из **текущего** `02_merged.npy`. Простого «перевыворота» тетов недостаточно: инвертированный
  Taubin'ом тет геометрически перекрывает соседей — смена winding чинит знак якобиана, но не
  устраняет наложение; нужна именно перегенерация с guard'ом.

### Поверхности — вердикт по тканям (`mc_mesh_check.py` + `validation_report.txt`)

| Ткань | Watertight | genus | Комп. | MaxErr, мм | Вердикт (surface-MC) |
|-------|-----------|-------|-------|-----------|----------------------|
| Мозг: WhiteMatter | да | 70 | 1 | 11.0 | ремонт (высокий genus) |
| Мозг: Skin | да | 49 | 1 | 9.2 | ремонт |
| Мозг: CSF / GrayMatter | да | 117 / 128 | 1 | 16.9 / 17.0 | ремонт (тонкие складчатые оболочки) |
| Мозг: Skull | да | 43 | 1 | 43.8 | ремонт |
| Мозг: Fat / Muscle / Dura / Vessels | да | 14 / 8 / 10 / 4 | 1 | **98 / 143 / 133 / 81** | **блокер** (геометрия разрушена B6) |
| Мышь: medulla / cerebellum | да | 0 | 1 | 0.0 | **готово** |
| Мышь: cerebrum | да | 0 | **2** | 0.0 | готово — 2 компоненты = левое/правое полушарие (genus 0 каждая) |
| Мышь: olfactory bulbs | да | 0 | 1 | 0.8 | ремонт (нормали внутрь — `orient=in`) |
| Мышь: skin | да | 4 | 1 | 5.9 | ремонт (genus; нормали внутрь) |
| Мышь: skeleton | да | 4 | 1 | **16.0** | ремонт (B6 `joincomp`) |

Компактные ткани (medulla, cerebellum, cerebrum) — готовы; крупные/тонкие/фрагментированные
(Fat, Muscle, Dura, Vessels, Skull, CSF, skeleton) — искажены пост-обработкой и непригодны для
surface-MC как есть (R1). **Для MMC это неблокирующе** — используется тет-сетка.

### Формат для решателя
Текущие `.mesh` (Medit) и `.vtk` не читаются MMC напрямую. Добавлен экспортёр в tetgen
`.node/.elem` (`mc_mesh_check.py --export-mmc`): 1-индексация, гарантированно положительная
ориентация каждого тета (инвертированные перевыворачиваются свопом `[0,1,3,2]`), тег региона
в `.elem` — формат MMC / iso2mesh. Плюс шаблон таблицы `label → (μ_a, μ_s, g, n)`
(`--props` → `optical_properties.csv`) для сшивки геометрии с оптикой (в 1/мм, согласовано с мм).

---

## 5. Рекомендации, НЕ реализованные (приоритезированы)

- **R0 (BLOCKER, операционный).** Перегенерировать сетку мыши Tier B из **текущего**
  `02_merged.npy` guard'ированным сглаживанием (или `cgal_smooth_iterations=0`), затем
  `mc_mesh_check.py --export-mmc`. Ожидаемо: 0 инверсий, актуальные материалы. Не сделано здесь:
  прогон CGAL ~20–60 мин, требует `cgal_env`/pygalmesh.
- **R1 (HIGH).** Переработать `validate_conformal_surfaces`: `pymeshfix.repair(joincomp=False)`,
  ремонт по-компонентно, децимация — относительная (не фикс. `target_faces`) либо вовсе отказ от
  децимации поверхностей (MMC они не нужны). Причина: `joincomp=True` даёт MaxErr до 143 мм (B6).
- **R2 (MED).** В вердикт `validate_conformal_surfaces` включить genus (|Euler−2|) и MaxErr, а не
  только watertight (B7). Частично закрыто `mc_mesh_check.py`.
- **R3 (MED).** В Tier B — контроль слайверов (min dihedral) и опциональный CGAL `perturb/exude`:
  для MMC качество тетов влияет на скорость трассировки (мышь: 5 слайверов, q_min 0.001).
- **R4 (MED).** Развести конфиги мозг/мышь (S4) и/или научить `parse_info_txt` формату MINC
  (`zspace/yspace/xspace … step … start`), чтобы CRISP читался универсально.
- **R5 (LOW).** `min_voxels` сделать per-tissue или защитить список «важных мелких тканей»
  (S5: eye, adrenal glands) от удаления.
- **R6 (LOW).** Убрать мёртвый код (S1, S2, S6), захардкоженный `HIERARCHY` (S3) заменить чтением
  вложенности из конфига/INFO; архивировать legacy-скрипты в отдельную папку.
- **R7 (LOW).** `merge_small_regions`: DT может присвоить остров-регион далёкой ткани через фон —
  рассмотреть ограничение радиуса присвоения.

---

## 6. Внесённые изменения (атомарные коммиты)

1. `fix(cgal): stop Tier A crash and Tier B tet inversions` — B1 + B2 (guard от инверсий;
   проверено: 222→0 на реальной сетке).
2. `fix(closer): canonicalize original labels before majority restore` — B3.
3. `fix(connectivity): use dy (not dz) for Y-crop log extent` — B5.
4. `fix(logging): keep Unicode log output from crashing on cp1251/cp1252` — B4.
5. `feat(mc): add Monte-Carlo mesh suitability checker + MMC export` — `mc_mesh_check.py`.

Проверки: `python -m py_compile` в **обоих** окружениях (проектный env 3.13, `cgal_env` 3.10) —
чисто; `mc_mesh_check.py` прогнан на мозге (MC-READY) и мыши (NOT MC-READY, 286 инверсий);
guard проверен эмпирически (222→0). Публичные форматы `INFO.txt` и `pipeline_config*.json`
не менялись. Windows-совместимость сохранена.
