# ADR-002 (v1.0): Второй вид — ежедневная аналитика за 4 недели (`analytics.html`)

**Статус:** accepted
**Версия:** v1.0.
**Область:** новый self-contained HTML-вид `analytics.html` + сопутствующие модули `build_analytics.py` / `render_analytics.py`. Расширяет snapshot в `analytics.py` секцией `daily`. НЕ трогает существующий `dashboard.html`, `render_dashboard.py`, `build_dashboard.py`, шаблоны слоёв и правила §2.2 ADR-001 (импорт `render → analytics` однонаправленный, snapshot — единственный контракт).

---

## 1. Контекст

Текущий `dashboard.html` показывает **4 недели в виде grouped-bars** (W-3..W-0, Пн..Вс). Этого хватает для «эта неделя vs прошлая», но не отвечает на:

- **Day-of-week pattern:** всегда ли Пн горячее Ср? Какой день недели самый «жёсткий»?
- **Rolling trend:** последние 7 дней vs предыдущие 7 — растёт расход или падает?
- **Skip-аналитика:** были ли в последних 4 неделях «нулевые» дни (runtime не работал) и где они сгруппированы.

Существующий weekly chart не даёт ответов: 4 группы по 7 столбцов скрывают и паттерн, и rolling-сравнение.

`compute_weekly` (analytics.py) уже агрегирует по дням — daily-вью может опираться на тот же `aggregate_by_hour` (без нового SQL) и на тот же L1..L4-palettе из карточки «24H Stream». Это естественное расширение, не новый домен.

Более сложные варианты (line-chart с rolling-7, dual-pane сравнение week-over-week, drill-down на конкретный день) рассмотрены и **отклонены** — см. §4.

## 2. Решение

### 2.1 Целевая структура

```
build_analytics.py        # entry: CLI + main (по образцу build_dashboard.py, новые --out/--db)
render_analytics.py       # render(snapshot) -> str; использует ТОЛЬКО snapshot["daily"] + константы
analytics.py              # + секция Database Access: derive из hourly (нового SQL нет)
                          # + секция Business Logic: compute_daily_4w, _daily_intensity
                          # + секция Snapshot Builder: "daily": {...} в build_snapshot
                          # + dataclass DailyBar (по соседству с HourlyBar/Week)
analytics.html            # выход build_analytics.py (gitignored, как dashboard.html)
tests/test_daily_4w.py    # unit-тесты compute_daily_4w / _daily_intensity
```

**Что НЕ создаём:** `analytics/`, `core/`, `domain/`, dataclass-snapshot, `export_json.py`, новые таблицы/индексы, отдельный SQL поверх `local_runtime_token_usage` — ежедневка считается из уже агрегированного `aggregate_by_hour` (тот же `dict[(date, hour) -> tokens]`).

### 2.2 Правила слоёв (наследуются из ADR-001 §2.2, без изменений)

1. `render_analytics` **никогда** не открывает SQLite. Импортирует из `analytics` только константы/типы/формат-хелперы (`WEEKDAY_LABELS`, `fmt_tokens`, `DailyBar`).
2. `analytics` **никогда** не генерирует HTML/CSS/JS.
3. `build_analytics` — entry only, без бизнес-логики.
4. Snapshot — единственный контракт между analytics и render. `render_analytics.render(snapshot)` принимает ровно один аргумент.
5. Любая будущая вьюха (JSON, CSV) потребляет snapshot, а не БД.

### 2.3 Внутренняя архитектура `analytics.py` (только дельта)

**Constants & Domain Types:** новый dataclass `DailyBar` рядом с `HourlyBar` / `Week`:

```python
@dataclass(frozen=True)
class DailyBar:
    """Один день в 4-недельной daily-view (calendar heatmap)."""
    date: date           # MSK-дата дня
    value: int | None    # None = empty (прошедший день без данных) или future
    state: str           # "active" | "current" | "future" | "empty"
    intensity: str | None  # "L1" | "L2" | "L3" | "L4" | None
    weekday: int         # 0..6 (Пн..Вс, iso weekday - 1)
    iso_week: int
    is_current_week: bool
```

Состояния (взаимоисключающие):

- `"future"` — `date > today` (ещё не наступил).
- `"current"` — `date == today` (копит, intensity = `_daily_intensity(value, sorted)` если `value > 0`, иначе `None`).
- `"empty"` — `date < today` И `value is None` (день прошёл, данных нет). Intensity = `None`.
- `"active"` — `date < today` И `value is not None`. Intensity = `_daily_intensity(value, sorted)`.

**Database Access:** нового SQL **нет**. `aggregate_by_hour` уже возвращает `dict[(date, hour) -> tokens]`; `compute_daily_4w` суммирует 24 часа на каждую дату — точно так же, как `compute_weekly` сегодня (см. analytics.py:449-453, паттерн `sum(hourly.get((day_date, h), 0) for h in range(24))`). Это сознательно: один путь агрегации = один источник истины.

**Business Logic:**

- `compute_daily_4w(hourly, today, week_count=WEEK_COUNT) -> list[DailyBar]` — 4 недели (Mon..Sun × 4 недели), oldest-first. Edge cases: empty day, today, future days текущей недели.
- `_daily_intensity(value, sorted_active) -> str | None` — аналог `_intensity_level` из карточки 24H, но для daily-диапазона. Квартили считаются по всем ненулевым `value` среди 28 дней (НЕ только по прошлым — current включается в распределение, иначе текущий день «выпадает» из шкалы). При `n < 4` — collapse в L2 (как в 24H).
- `pill_level` — переиспользуем без изменений (порог для `daily.weekly_burn_level` если TL захочет выводить «сегодня в топ-25% недели»).

**Snapshot Builder:** новая секция в `build_snapshot`:

```python
"daily": {
    "since": date,                 # понедельник W-3 (старт окна 4 недель)
    "weeks": list[DailyBar],       # ровно 28 (4 ISO-недели × 7 дней, фиксировано)
    "current_weekday": int,        # 0..6 (Пн..Вс) — для подсветки колонки текущей недели
    "weekly_cap": int,             # WEEKLY_CAP_TOKENS (для burn-rate context, не для pill)
    "burn_today": str,             # "ok" | "warn" | "over" | "none" (pill_level сегодняшнего дня vs 7-day avg)
    "burn_7d_avg": int | None,     # среднее токенов/день за последние 7 дней (для контекста burn)
}
```

> **Примечание:** `burn_today` — это `pill_level(today_value, 7d_avg)` при `7d_avg > 0`, иначе `"none"`. Семантика: «сегодня расходуется vs средний день последних 7 дней». (`7d_avg` — это `burn_7d_avg` ниже, per-day; `* 7` НЕ используется — `today_value` это один день, его и сравниваем с дневной нормой.) TL может убрать из рендера, если не нужно — поле готовится в snapshot, а рендер решает, показывать ли. Это сознательный «мини-extra cost, max-опция для UI».

### 2.4 Контракт `snapshot["daily"]` — полный

```python
snapshot["daily"] = {
    "since": date,                  # понедельник W-(week_count-1) MSK
    "weeks": [DailyBar × 28],       # все дни в окне, oldest-first, по возрастанию date
    "current_weekday": int,         # 0..6 (Пн..Вс, MSK)
    "weekly_cap": int,              # WEEKLY_CAP_TOKENS
    "burn_today": str,              # "ok" | "warn" | "over" | "none"
    "burn_7d_avg": int | None,      # None если 7d_avg == 0
}
```

`DailyBar` (см. §2.3): все 4 неполных/полных недели, oldest → newest. Колонка «current week» — последние 7 (или меньше, если today=Mon, тогда 1..7). Колонка «W-3» — самые старые 7.

**Семантика «since = понедельник W-3»:** окно ровно 4 недели Пн..Вс, выровнено по ISO-неделям. Альтернатива «today − 27 days» отклонена: даёт 28 дней, но не выровнено по неделям — на heatmap'е ячейки «плавают» (Пн прошлой недели ≠ Пн позапрошлой). Для day-of-week pattern выравнивание по строкам = Пн..Вс критично.

**Длина массива `weeks`:** 28 базовый случай. Если `since` Пн и сегодня Вс — ровно 28. Если `since` Пн и сегодня Ср текущей недели — 28 + 3 будущих = 28 (будущие в текущей неделе не расширяют окно). Если `since` Пн и сегодня Пн — 28 (текущая неделя = 1 день + 6 future). Окно всегда 4 ISO-недели = 28 элементов. **NB:** в месяце, где 5 недель, окно всё равно 4, а не «последние 28 дней с произвольной границей». Это сознательно.

**Что входит в `compute_daily_4w`, а что нет:**

- ✅ Сумма за день (24 часа) для каждой даты в окне.
- ✅ State (active/current/future/empty) и intensity по квартилям.
- ✅ is_current_week / iso_week / weekday.
- ❌ Week-over-week delta (отклонено, см. §4).
- ❌ Rolling 7-day average — отдельной секцией не выделяем, считается inline в `build_snapshot` для `burn_7d_avg`.

### 2.5 Визуальное направление: **calendar heatmap 7×4**

```
        W-3        W-2        W-1        W0
Пн    [L2 4.2M]  [L3 7.1M]  [L1 1.8M]  [   —   ]  ← если будущее
Вт    [L4 9.0M]  [L2 3.5M]  [L2 3.2M]  [   —   ]
Ср    [L1 2.0M]  [L4 8.8M]  [L3 5.6M]  [L3 5.0M]  ← current
Чт    [L3 6.5M]  [L1 1.0M]  [L2 4.0M]  [   —   ]  ← future (dashed)
Пт    [L2 4.1M]  [L2 3.9M]  [L4 9.2M]  [   —   ]
Сб    [L1 0.8M]  [L1 1.2M]  [L1 0.5M]  [   —   ]
Вс    [   —   ]  [   —   ]  [L1 1.5M]  [   —   ]
       (empty)    (empty)
```

- 4 столбца (W-3 → W-0), 7 строк (Пн..Вс). Текущая неделя — последний столбец, с outline.
- Каждая ячейка: фон по intensity (L1..L4, та же палитра, что в 24H Stream), внутри — крупная цифра (`fmt_tokens(value)`), под ней — мелкая подпись `DD.MM` (если будущее/empty — прочерк).
- Empty/future — пунктирная граница, без фона (`#1f2933 30%`).
- Tooltip (`title=` attribute): `DD.MM.YYYY, Пн — 4.20M (L2)`.
- Под сеткой: meta-строка «4 недели · с Пн DD.MM · burn сегодня: ok/warn/over».

**Почему heatmap, а не line-chart:** day-of-week pattern читается с одного взгляда (строка = weekday, столбец = week), 28 точек помещаются в одну карточку (~480×280px), палитра L1..L4 уже зафиксирована в карточке «24H Stream» (см. PRD §6.6/§7.4/§8 FR-9) — повторное использование тех же 4 уровней для heatmap'а даёт визуальную консистентность. Weekly chart остаётся grouped-bars (своя шкала), его в heatmap-палитру не втягиваем. Line-chart отклонён: rolling-trend читается хуже (4 недели — короткий горизонт, шумный), требует второй координаты (Y-ось) и не показывает паттерн по дням недели.

### 2.6 `render_analytics.py` (тонкий модуль, как `render_dashboard.py`)

- Импортирует ТОЛЬКО `DailyBar`, `WEEKDAY_LABELS`, `fmt_tokens` из `analytics`. Никаких compute-функций.
- Функции: `_render_daily_card(snapshot) -> str`, `_render_daily_cell(bar, is_current_week) -> str`, `_render_daily_grid(snapshot) -> str`, `render(snapshot: dict) -> str`.
- Inline CSS (как в `render_dashboard.py`): новые классы `.daily-grid`, `.daily-cell`, `.daily-cell.future`, `.daily-cell.empty`, `.daily-cell.current-week`, `.daily-cell.intensity-L{1..4}`. Палитра L1..L4 — **общая** с 24H Stream (выносить в shared-css — не задача этого ADR).
- HTML-каркас: минимальный (как `dashboard.html` сейчас — `<!DOCTYPE html>`, `<head>`, `<meta http-equiv="refresh" content="60">`, `<body>` с одной карточкой). Никаких `<script>` пока — heatmap статичен.

### 2.7 `build_analytics.py` (entry, по образцу `build_dashboard.py`)

- `DB_PATH` (default — тот же путь, что в `build_dashboard.py`), `OUTPUT_PATH` (default `analytics.html` рядом со скриптом).
- `parse_args`: `--db PATH`, `--out PATH`, `--no-write`, `--quiet`. Имена и сигнатуры — **клон** `build_dashboard.parse_args` (consistency).
- `main()`: открыть БД → `analytics.build_snapshot(args.db, now_msk=datetime.now(MSK))` → лог из `snapshot["daily"]` (since, weeks count, burn_today, burn_7d_avg) → `html = render_analytics.render(snapshot)` → запись/stdout.

## 3. План миграции и приёмка

**Шаг 1.** `analytics.py` — `DailyBar`, `_daily_intensity`, `compute_daily_4w`, секция `daily` в `build_snapshot`. Покрытие: `tests/test_daily_4w.py` (≥ 12 кейсов: empty day, today, future, 28-day alignment, current-week highlight, intensity quartiles, edge — today = Mon, today = Sun).

**Шаг 2.** `render_analytics.py` — `render(snapshot)`, `_render_daily_card`, `_render_daily_grid`, `_render_daily_cell`. Без тестов на байт-идентичность (новый файл, рефа нет). Sanity-проверка: `python build_analytics.py` → `analytics.html` существует, открывается в браузере, видна 7×4 сетка.

**Шаг 3.** `build_analytics.py` — CLI + main + лог.

**Шаг 4.** README — секция «Project layout» + краткое описание `analytics.html` в «What it does». Без переписывания остального.

**Приёмка каждого шага:**

1. `python -c "import analytics, render_analytics, build_analytics"` — нет циклических импортов, нет `NameError`.
2. Все 7+ существующих тест-файлов + новый `test_daily_4w.py` — pass. Суммарный счёт растёт с 114 до ≥ 126.
3. Боевая БД: `python build_analytics.py` → `analytics.html` создан, в нём 28 ячеек (4 столбца × 7 строк) + meta-строка, открывается в браузере без ошибок.
4. `python build_dashboard.py` и `python build_analytics.py` можно запускать независимо (разные snapshot'ы — каждый entry зовёт `build_snapshot` отдельно; общий SQL закэшировать — **не** в этой задаче, см. §4).
5. `analytics.html` не зависит от `dashboard.html` (можно удалить один — другой работает).
6. Stdlib only, Python 3.9+, `from __future__ import annotations` в новых модулях.

## 4. Анти-требования (не делать)

- ❌ Пакеты и каталоги (`analytics_views/`, `daily/`, `core/`); новые файлы сверх 4 из §2.1.
- ❌ Dataclass-snapshot (триггер из ADR-001 §2.2 rule 6 — два потребителя есть, но формат dict-а держится на тестах; при третьем — замена).
- ❌ Новый SQL поверх `local_runtime_token_usage` (всё считается из `aggregate_by_hour`).
- ❌ Line-chart / area-chart / bar-chart — heatmap покрывает основной вопрос (day-of-week pattern). Если понадобится trend — отдельный ADR.
- ❌ Week-over-week delta (среднее W-0 vs W-1) — лишнее усложнение, heatmap даёт это визуально.
- ❌ Drill-down / popup / `<script>` для интерактивности — heatmap статичен; tooltips через `title=`.
- ❌ JSON / CSV экспорт — отдельная задача при появлении потребителя (ADR-001 §6.1).
- ❌ Правки в `render_dashboard.py` / `build_dashboard.py` / `dashboard.html` — Path B не трогает существующий вид.
- ❌ Кэширование SQL между двумя build-entry (сейчас каждый зовёт `aggregate_by_hour` отдельно — для 4-недельного окна это <100 мс, оптимизация — не в этой задаче).
- ❌ Перенос `WEEK_COUNT` / `WEEKDAY_LABELS` / палитры L1..L4 в shared-constants (текущая дублированная константа `_HOUR_STATE_LEVELS` уже есть; вынос — отдельный тикет, не блокирует).

## 5. Последствия

**+** Второй вид подключается без дублирования compute; day-of-week pattern читается с одного взгляда; rolling 7-day burn виден в meta-строке; ADR-001 правила слоёв соблюдены (нет циклических импортов, snapshot — единственный контракт, render без SQL); существующий `dashboard.html` и его тесты не тронуты.

**−** `analytics.py` вырастет на ~120–150 строк (851 → ~990). До триггера §6 ADR-001 (1300) остаётся ~300 строк — комфортно. Если в будущем добавится третий вид — распил на `analytics/` пакет по секциям (ADR-001 §6.2).

## 6. Будущая работа (вне этого ADR, для контекста)

1. Вынос shared-палитры L1..L4 и общих CSS-классов в `render_common.py` (или аналог) — когда появится третий вид или явный визуальный drift.
2. Расширение `aggregate_by_hour` индексами при росте БД >100K строк (беклог, см. ADR-001 §6.5).
3. JSON-экспорт `snapshot` (`export_json.py`) — при появлении потребителя (CI, дашборд-агрегатор).
4. Drill-down: клик по ячейке heatmap'а → модалка с 24-часовой разбивкой выбранного дня (через `snapshot["daily"]` + `aggregate_by_hour`).
