"""build_dashboard.py — static token-usage dashboard generator.

Читает `local_runtime_token_usage` из runtime-state.sqlite и собирает
self-contained `dashboard.html` (без backend, без внешнего JSON).

Спецификация: prd-token-dashboard-prototype.md
Запуск:  python build_dashboard.py
Опции:   --db <path>     путь к sqlite (по умолчанию DB_PATH ниже)
         --out <path>    путь к выходному HTML (по умолчанию OUTPUT_PATH ниже)
         --no-write      не записывать файл (dry-run, печатает в stdout)
         --quiet         не печатать лог

Расписание: Windows Task Scheduler, раз в 5 минут.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---- constants -------------------------------------------------------------

# Абсолютные пути по умолчанию — рядом со скриптом. Переопределяются --db/--out.
# DB_PATH: Path = Path(__file__).resolve().parent / "runtime-state.sqlite"
DB_PATH: Path = Path("C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite")
OUTPUT_PATH: Path = Path(__file__).resolve().parent / "dashboard.html"

# Europe/Moscow = UTC+3 круглый год (с 2014 без перехода на зимнее время).
# Хардкод константой, как и просили — без zoneinfo-зависимостей.
MSK = timezone(timedelta(hours=3))

# 5-часовые слоты (полу-открытые [start, end)). 4 дневных по 5 часов + 1 ночной
# 4 часа (23:00 вчера → 03:00 сегодня, переход через полночь). PRD §6.3.
# Лейблы — half-open: "03:00–08:00" значит [3, 8), т.е. часы 3,4,5,6,7.
# Ночной слот асимметричен (4h) — сознательно, не добиваем до 5h через 22 или 3.
WINDOWS: list[dict] = [
    {"name": "morning",   "hours": [3, 4, 5, 6, 7],     "label": "03:00–08:00", "wraps": False},
    {"name": "midday",    "hours": [8, 9, 10, 11, 12],  "label": "08:00–13:00", "wraps": False},
    {"name": "afternoon", "hours": [13, 14, 15, 16, 17], "label": "13:00–18:00", "wraps": False},
    {"name": "evening",   "hours": [18, 19, 20, 21, 22], "label": "18:00–23:00", "wraps": False},
    {"name": "night",     "hours": [23, 0, 1, 2],       "label": "23:00–03:00", "wraps": True},
]
NIGHT_SLOT = WINDOWS[4]
WEEK_COUNT: int = 4                                # PRD §6.4
WEEKDAY_LABELS: tuple[str, ...] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Недельная квота (cap) на input+output токены. На текущий день weekly chart
# выводится красная пунктирная полоска с подписью — это «потолок» сегодняшнего
# расхода, ниже которого нужно остаться, чтобы уложиться в капу за 7 дней
# (включая сегодня). Если сегодня превысил — порог автоматически пересчитается
# на завтра (формула зависит от today_spent и days_left, оба обновляются).
WEEKLY_CAP_TOKENS: int = 75_000_000

# ---- domain types ----------------------------------------------------------

@dataclass(frozen=True)
class Week:
    """Одна неделя для grouped-bar chart."""
    label: str          # "W-32"
    monday: date        # понедельник этой недели (MSK)
    days: list[int | None]   # 7 значений Пн..Вс, None = disabled/no data
    is_current: bool


# ---- CLI -------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сгенерировать self-contained dashboard.html из runtime-state.sqlite.",
    )
    p.add_argument("--db", type=Path, default=DB_PATH,
                   help=f"Путь к SQLite (default: {DB_PATH})")
    p.add_argument("--out", type=Path, default=OUTPUT_PATH,
                   help=f"Путь к выходному HTML (default: {OUTPUT_PATH})")
    p.add_argument("--no-write", action="store_true",
                   help="Не записывать файл (dry-run, печатает в stdout).")
    p.add_argument("--quiet", action="store_true",
                   help="Не печатать лог сборки.")
    return p.parse_args()


# ---- IO --------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection:
    """Открыть SQLite в режиме read-only через URI.

    mode=ro гарантирует, что мы не сможем случайно писать в базу
    и не заблокируем писателей runtime'а.
    """
    if not path.exists():
        raise FileNotFoundError(f"SQLite не найден: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    return con


def aggregate_by_hour(con: sqlite3.Connection, since_msk_date: date) -> dict[tuple[date, int], int]:
    """SELECT (date, hour, sum_tokens) GROUP BY date,hour, начиная с since_msk_date (MSK).

    Возвращает {(msk_date, msk_hour): tokens}, где tokens = input + output.
    Cache/reasoning/cost намеренно исключены (PRD §5).
    """
    since_msk_midnight = datetime.combine(since_msk_date, datetime.min.time(), tzinfo=MSK)
    since_ts_ms = int(since_msk_midnight.timestamp() * 1000)

    # Преобразование ms→MSK делаем в SQLite: '+3 hours' — детерминированно,
    # не зависит от локали машины. strftime('%H') → строка, кастуем в INT.
    sql = """
        SELECT
            date(ts / 1000, 'unixepoch', '+3 hours')                          AS msk_date,
            CAST(strftime('%H', ts / 1000, 'unixepoch', '+3 hours') AS INT)  AS msk_hour,
            COALESCE(SUM(input_tokens + output_tokens), 0)                    AS tokens
        FROM local_runtime_token_usage
        WHERE ts >= ?
        GROUP BY msk_date, msk_hour
    """
    out: dict[tuple[date, int], int] = {}
    for d_str, h, tokens in con.execute(sql, (since_ts_ms,)):
        out[(date.fromisoformat(d_str), int(h))] = int(tokens)
    return out


# ---- aggregations ----------------------------------------------------------

def compute_current_hour(hourly: dict[tuple[date, int], int], today: date) -> int:
    """Токены за текущий календарный час (MSK). 0 если данных нет."""
    now_msk = datetime.now(MSK)
    return hourly.get((today, now_msk.hour), 0)


def compute_today(hourly: dict[tuple[date, int], int], now_msk: datetime) -> int:
    """Токены с начала суток (00:00) до текущего часа включительно (MSK).

    Running total: включает in-progress current_hour, поэтому значение
    "допрыгивает" в течение последнего часа. Это сознательно — карточка
    "Токены / сегодня" по своей природе дубль части графика окна (по
    запросу TL: "просится карточка, даже если это дубль графика").
    """
    today = now_msk.date()
    return sum(hourly.get((today, h), 0) for h in range(now_msk.hour + 1))


def current_window(now_msk: datetime) -> dict:
    """Какой 5h-слот активен сейчас (MSK).

    Дневные слоты [3, 8), [8, 13), [13, 18), [18, 23) — half-open: старший час
    входит в предыдущий слот. Ночной слот [23, 3) — оборачивает полночь
    (23:00 одного дня + 0..2:00 следующего).
    """
    h = now_msk.hour
    if h >= 23 or h < 3:
        return NIGHT_SLOT
    idx = (h - 3) // 5  # 0..3
    return WINDOWS[idx]


def compute_current_window(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> tuple[int, list[tuple[int, int, date]], str]:
    """Сумма + per-hour (с датами) + лейбл для активного слота.

    Для ночного слота час 23 берётся из (today - 1 day), часы 0..2 — из today.
    Для остальных слотов все часы — из today.
    """
    today = now_msk.date()
    window = current_window(now_msk)
    entries: list[tuple[int, int, date]] = []
    if window["wraps"]:
        yesterday = today - timedelta(days=1)
        for h in window["hours"]:
            d = yesterday if h == 23 else today
            entries.append((h, hourly.get((d, h), 0), d))
    else:
        for h in window["hours"]:
            entries.append((h, hourly.get((today, h), 0), today))
    total = sum(v for _, v, _ in entries)
    return total, entries, window["label"]


def compute_weekly(
    hourly: dict[tuple[date, int], int], today: date, week_count: int = WEEK_COUNT
) -> list[Week]:
    """Последние `week_count` недель, oldest-first.

    Логика disabled-баров (None):
      - day_date > today           → None (будущее, данных быть не может)
      - day_date < today и count=0 → None (прошедший день, но логов нет — нет данных)
      - day_date == today          → реальная сумма (может быть 0, "только начался")
    """
    iso = today.isocalendar()  # (iso_year, iso_week, iso_weekday 1..7)
    current_monday = today - timedelta(days=iso[2] - 1)

    weeks: list[Week] = []
    for i in range(week_count):
        # weeks[0] = самая старая, weeks[-1] = текущая
        offset = week_count - 1 - i
        monday = current_monday - timedelta(weeks=offset)
        label = f"W-{monday.isocalendar()[1]}"
        is_current = (monday == current_monday)

        days: list[int | None] = []
        for d_idx in range(7):
            day_date = monday + timedelta(days=d_idx)
            if day_date > today:
                # Будущий день (любой недели) — данных быть не может
                days.append(None)
                continue
            # Прошедший день или сегодня: если ни одной строки в БД за этот день
            # (любой час) — None (no data yet), иначе реальная сумма.
            has_any = any((day_date, h) in hourly for h in range(24))
            if not has_any:
                days.append(None)
                continue
            days.append(sum(hourly.get((day_date, h), 0) for h in range(24)))
        weeks.append(Week(label=label, monday=monday, days=days, is_current=is_current))
    return weeks


def compute_weekly_threshold(
    weekly_cap: int, today_spent: int, days_left: int
) -> int | None:
    """«Потолок» расхода на сегодня (накопительно), чтобы уложиться в weekly_cap.

    Формула:  threshold = max(0, (cap − today_spent) / days_left)  (с floor).

    Семантика:
      - threshold — это максимум, который можно потратить СЕГОДНЯ (с начала суток
        до конца дня), чтобы при равномерном расходе на оставшиеся дни общая
        сумма за неделю не превысила `weekly_cap`.
      - Если сегодня уже потратил больше, чем threshold, — завтра формула
        пересчитается (today_spent станет больше, days_left меньше → новый
        порог). Это и есть «если превысил — на следующий день уровень
        пересчитается».
      - days_left включает сегодня:  Пн=7, Вт=6, …, Вс=1. Считается как
        `8 − isoweekday(today)`.

    Возвращает:
      - int ≥ 0 — сам threshold (clamped в 0 снизу для консервативности).
      - None   — если `days_left <= 0` (нечего считать; защита от деления на 0).
                 На UI такие случаи маловероятны (текущий день всегда ≥ 1),
                 но контракт это явно фиксирует.

    Параметры намеренно плоские (без `now_msk`/SQLite) — функция чистая,
    тестируется без моков. Вызов из main() подставляет реальные числа.
    """
    if days_left <= 0:
        return None
    remaining = weekly_cap - today_spent
    if remaining <= 0:
        # Вся капа уже исчерпана (или превышена) — сегодня больше тратить не надо.
        return 0
    # floor вниз: лучше показать чуть заниженный порог, чем подтолкнуть к
    # превышению. 10.71M → 10M, не 11M.
    return remaining // days_left


def compute_sparkline_current(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> list[int]:
    """Sparkline для KPI «Текущий момент»: последние до 8 часов (h-7..h) сегодня.

    Если now.hour < 7, начинаем с 00:00 — будет короче 8 точек, что ок
    (полилиния рендерится с тем количеством точек, что есть).
    """
    today = now_msk.date()
    h = now_msk.hour
    start = max(0, h - 7)
    return [hourly.get((today, hr), 0) for hr in range(start, h + 1)]


def compute_sparkline_today(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> list[int]:
    """Sparkline для KPI «Сегодня»: часы 00:00..h сегодня (running series)."""
    today = now_msk.date()
    h = now_msk.hour
    return [hourly.get((today, hr), 0) for hr in range(h + 1)]


def compute_sparkline_window(
    window_entries: list[tuple[int, int, date]],
) -> list[int]:
    """Sparkline для KPI «Рабочее время»: почасовые значения активного слота.

    Порядок соответствует порядку entries (для ночного 23, 0, 1, 2;
    для дневных — по возрастанию). 4-5 точек.
    """
    return [v for _, v, _ in window_entries]


def compute_prev_hour(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> int | None:
    """Токены за предыдущий час (h-1). На границе суток — вчерашний 23:00."""
    h = now_msk.hour
    today = now_msk.date()
    if h == 0:
        return hourly.get((today - timedelta(days=1), 23))
    return hourly.get((today, h - 1))


def compute_prev_day_today(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> int | None:
    """Вчерашний running-total до того же часа (00:00..h включительно)."""
    yesterday = now_msk.date() - timedelta(days=1)
    h = now_msk.hour
    return sum(hourly.get((yesterday, hr), 0) for hr in range(h + 1))


def compute_prev_window_total(
    hourly: dict[tuple[date, int], int], now_msk: datetime
) -> int:
    """Токены за предыдущий 5h-интервал (тот, что был до текущего слота).

    Соседство слотов:
      morning ← night   (ночной оборачивает полночь: 23 вчера + 0,1,2 сегодня)
      midday  ← morning
      afternoon ← midday
      evening ← afternoon
      night   ← evening (предыдущий вечер — это вчерашний 18..22)
    """
    cur = current_window(now_msk)
    today = now_msk.date()
    yesterday = today - timedelta(days=1)
    name = cur["name"]
    if name == "morning":
        return (
            hourly.get((yesterday, 23), 0)
            + sum(hourly.get((today, hr), 0) for hr in (0, 1, 2))
        )
    if name == "night":
        return sum(hourly.get((yesterday, hr), 0) for hr in (18, 19, 20, 21, 22))
    # Дневные слоты: предыдущий слот — целиком сегодня.
    idx = next(i for i, w in enumerate(WINDOWS) if w["name"] == name)
    prev_slot = WINDOWS[idx - 1]
    return sum(hourly.get((today, hr), 0) for hr in prev_slot["hours"])


# ---- formatting ------------------------------------------------------------

def fmt_tokens(n: int | None) -> str:
    """182500 → '182.5K', 1234567 → '1.23M', None → '—'."""
    if n is None:
        return "—"
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def fmt_int(n: int | None) -> str:
    """Без K/M, для осей и лейблов."""
    return "—" if n is None else f"{n:,}".replace(",", " ")


def fmt_delta_pct(curr: int, prev: int | None) -> str | None:
    """'+8%' / '−3%' / None если нет baseline (prev None или 0).

    Используется для бейджа .delta на KPI-карточках. None означает "без бейджа".
    Используем минус U+2212, не дефис — чтобы не лип к цифре в шрифтах.
    """
    if prev is None or prev <= 0:
        return None
    pct = (curr - prev) / prev * 100
    if pct >= 0:
        return f"+{pct:.0f}%"
    return f"−{abs(pct):.0f}%"


def _bar_height_pct(value: int, scale: str, y_info) -> float:
    """Высота бара в % (2..100). 0 → 0 (рендерится как min-height floor)."""
    if value <= 0:
        return 0.0
    if scale == "log" and y_info is not None:
        y_min, y_max, _ = y_info
        if y_min <= 0 or y_max <= 0 or y_max <= y_min:
            return 0.0
        log_min = math.log10(y_min)
        log_max = math.log10(y_max)
        frac = (math.log10(value) - log_min) / (log_max - log_min)
        return max(2.0, min(100.0, frac * 100))
    if scale == "linear" and isinstance(y_info, int) and y_info > 0:
        frac = value / y_info
        return max(2.0, min(100.0, frac * 100))
    return 0.0


# ---- HTML rendering --------------------------------------------------------

# Текущая max-высота для шкалы weekly chart. Считаем от реальных значений,
# округляя вверх до удобного тика.
def _y_max_for(weeks: list[Week]) -> int:
    """Linear: max шкалы, округлённый вверх до удобного тика."""
    values = [d for w in weeks for d in w.days if d is not None]
    if not values:
        return 1000
    # Округляем max до ближайших 100K, минимум 200K.
    raw = max(values)
    if raw < 200_000:
        return 200_000
    step = 200_000 if raw <= 2_000_000 else 1_000_000
    return ((raw + step - 1) // step) * step


def _y_ticks_for_log(weeks: list[Week]) -> tuple[float, float, list[int]] | None:
    """Log: (y_min, y_max, tick_values) или None если нет positive-значений.

    Снэпим к степеням 10. Сверху даём минимум 1 decade headroom, чтобы
    самый большой бар не упирался в потолок шкалы.

    Защита: если 0-day попадёт (data с 0-токенами, но не None) — он
    отрендерится как 2px floor bar в render-функции, см. _bar_geometry.
    """
    values = [v for w in weeks for v in w.days if v is not None and v > 0]
    if not values:
        return None
    raw_min = min(values)
    raw_max = max(values)
    exp_min = int(math.floor(math.log10(raw_min)))
    exp_max_raw = int(math.floor(math.log10(raw_max)))
    # +1 decade headroom, чтобы max-бар не лип к потолку
    exp_max = exp_max_raw + 1
    if exp_max <= exp_min:
        exp_max = exp_min + 1
    y_min = 10 ** exp_min
    y_max = 10 ** exp_max
    ticks = [10 ** e for e in range(exp_min, exp_max + 1)]
    return (float(y_min), float(y_max), ticks)


def _render_sparkline(points: list[int], color: str) -> str:
    """SVG polyline для KPI-карточки. viewBox 0 0 100 40.

    X — равномерно 0..100, Y — нормализованное значение (0=низ, 40=верх SVG).
    Edge cases:
      - 0 точек: горизонтальная линия по центру
      - 1 точка: одна точка в центре
      - все значения равны: горизонтальная линия по середине (span=1 защита от /0)
    """
    if not points:
        return (
            f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="0,20 100,20" />'
            f"</svg>"
        )
    if len(points) == 1:
        return (
            f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="50,20" />'
            f"</svg>"
        )
    lo, hi = min(points), max(points)
    span = max(1, hi - lo)
    coords: list[str] = []
    for i, v in enumerate(points):
        x = (i / (len(points) - 1)) * 100
        y = 40 - ((v - lo) / span) * 40
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg viewBox="0 0 100 40" preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{" ".join(coords)}" />'
        f"</svg>"
    )


def _axis_labels_linear(y_max: int) -> str:
    """4 тика сверху вниз: y_max, 2/3, 1/3, 0."""
    labels = [y_max, y_max * 2 // 3, y_max // 3, 0]
    return "".join(f"<span>{fmt_int(v)}</span>" for v in labels)


def _axis_labels_log(log_info) -> str:
    """4 тика сверху вниз: ticks[-1]..ticks[0]. Если None — пусто."""
    if not log_info:
        return ""
    _, _, ticks = log_info
    return "".join(f"<span>{fmt_tokens(int(t))}</span>" for t in reversed(ticks))


def _render_weekly_grid(
    weeks: list[Week], scale: str, y_info, weekly_threshold: int | None = None
) -> str:
    """HTML-грид 4 недели × 7 дней. Недели — колонки, дни — бары.

    Классы баров:
      .bar.history — прошедшие/текущие данные (полупрозрачный белый)
      .bar.accent  — текущий день текущей недели (фиолетовый градиент)
      .bar.future  — будущие дни или None-данные (пунктир, opacity 0.7)

    Доп. элементы:
      .bar-cell      — обёртка вокруг бара (даёт position:relative для threshold)
      .threshold     — горизонтальная пунктирная линия «потолка» сегодняшнего дня
      .threshold-label — подпись справа от линии (например «порог 10.37M»)

    Threshold рисуется ТОЛЬКО над текущим днём текущей недели (W-N, сегодня).
    Если `weekly_threshold is None` (cap превышен или days_left=0) — линия
    не рисуется. Если today_spent > threshold, бар визуально выше линии —
    это и есть сигнал «превысил, на завтра уровень пересчитается».

    Высота — % от 260px контейнера (через _bar_height_pct).
    В шапке карточки — W-лэйбл слева + суммарный объём за неделю в M справа.
    """
    today_d = date.today()
    out: list[str] = []
    for week in weeks:
        bars: list[str] = []
        for d_idx in range(7):
            value = week.days[d_idx]
            day_d = week.monday + timedelta(days=d_idx)
            if day_d > today_d:
                cls = "bar future"
                height_pct = 0.0
                title_extra = "будущее"
            elif value is None:
                cls = "bar future"
                height_pct = 0.0
                title_extra = "нет данных"
            else:
                if week.is_current and day_d == today_d:
                    cls = "bar accent"
                else:
                    cls = "bar history"
                height_pct = _bar_height_pct(value, scale, y_info)
                title_extra = fmt_int(value)
            title = f"{week.label}, {WEEKDAY_LABELS[d_idx]}: {title_extra}"
            cell_inner = (
                f'<div class="{cls}" style="height:{height_pct:.1f}%" title="{title}"></div>'
            )

            # Threshold рисуем ТОЛЬКО для текущего дня текущей недели, и только
            # если он вычислился. Линия прибита к правому краю бара (через
            # inset:0 в CSS) и выровнена по высоте threshold-значения.
            if (
                weekly_threshold is not None
                and week.is_current
                and day_d == today_d
            ):
                thr_pct = _bar_height_pct(weekly_threshold, scale, y_info)
                thr_label = f"{fmt_tokens(weekly_threshold)}"
                cell_inner += (
                    f'<div class="threshold" style="bottom:{thr_pct:.1f}%" '
                    f'title="Потолок сегодня: {fmt_int(weekly_threshold)} токенов '
                    f'(weekly cap {fmt_int(WEEKLY_CAP_TOKENS)})">'
                    f'<span class="threshold-label">{thr_label}</span>'
                    f'</div>'
                )

            bars.append(f'<div class="bar-cell">{cell_inner}</div>')
        # Сумма за неделю — только по дням с данными (None — no data, не 0).
        week_total = sum(v for v in week.days if v is not None)
        week_total_str = f"{week_total / 1_000_000:.2f}M"
        week_cls = "week current" if week.is_current else "week"
        days_html = "".join(f"<span>{lbl}</span>" for lbl in WEEKDAY_LABELS)
        out.append(
            f'<div class="{week_cls}">'
            f'<div class="week-head">'
            f'<span class="week-label">{week.label}</span>'
            f'<span class="week-total" title="Сумма за {week.label}">{week_total_str}</span>'
            f'</div>'
            f'<div class="bars">{"".join(bars)}</div>'
            f'<div class="days">{days_html}</div>'
            f"</div>"
        )
    return "".join(out)


def render_html(
    *,
    current_hour_tokens: int,
    current_hour_delta: str | None,
    current_hour_sparkline: list[int],
    today_tokens: int,
    today_delta: str | None,
    today_sparkline: list[int],
    window_total: int,
    window_delta: str | None,
    window_sparkline: list[int],
    window_label: str,
    window_wraps: bool,
    weeks: list[Week],
    y_max: int,
    log_info: tuple[float, float, list[int]] | None,
    weekly_threshold: int | None,
) -> str:
    now_msk = datetime.now(MSK)
    today_label = now_msk.strftime("%Y-%m-%d %H:%M MSK")
    today_date = today_label[:10]
    current_week_label = weeks[-1].label if weeks else "—"

    # KPI time-labels
    cur_h = now_msk.hour
    cur_time = f"{cur_h:02d}:00–{cur_h:02d}:59, {today_date}"
    today_time = f"00:00–{cur_h:02d}:59, {today_date}"
    if window_wraps:
        window_time = f"{window_label}, вчера→сегодня"
    else:
        window_time = f"{window_label}, сегодня"

    # Sparklines: 3 цвета, как в concept-ops
    spark1 = _render_sparkline(current_hour_sparkline, "var(--accent-2)")
    spark2 = _render_sparkline(today_sparkline, "var(--accent)")
    spark3 = _render_sparkline(window_sparkline, "#8ea3c7")

    # Delta-бейджи: None → без бейджа; "−" (U+2212) → красный (delta--neg)
    def _delta_html(d: str | None) -> str:
        if d is None:
            return ""
        cls = "delta delta--neg" if d.startswith("−") else "delta"
        return f'<div class="{cls}">{d}</div>'

    delta1 = _delta_html(current_hour_delta)
    delta2 = _delta_html(today_delta)
    delta3 = _delta_html(window_delta)

    # Weekly chart — оба варианта (linear/log), видимость через data-scale на .chart-panel
    linear_grid = _render_weekly_grid(weeks, "linear", y_max, weekly_threshold)
    log_grid = (
        _render_weekly_grid(weeks, "log", log_info, weekly_threshold) if log_info else ""
    )
    axis_linear = _axis_labels_linear(y_max)
    axis_log = _axis_labels_log(log_info)

    # Meta-диапазон для log-варианта
    if log_info:
        log_meta_range = (
            f"{fmt_tokens(int(log_info[0]))} … {fmt_tokens(int(log_info[1]))}"
        )
    else:
        log_meta_range = "—"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Token Dashboard — {today_label}</title>
  <style>
    :root {{
      --bg: #0f1115;
      --panel: #181b22;
      --panel-2: #1d2129;
      --ink: #f5f7fb;
      --muted: rgba(216, 223, 236, 0.62);
      --line: rgba(255, 255, 255, 0.05);
      --grid: rgba(148, 163, 184, 0.12);
      --accent: #8b5cf6;
      --accent-2: #10b981;
      --history: rgba(255, 255, 255, 0.18);
      --history-2: rgba(255, 255, 255, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", "Segoe UI Variable", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.16), transparent 26%),
        linear-gradient(180deg, #0e1014 0%, #11141a 100%);
    }}
    .shell {{ width: min(1280px, calc(100vw - 36px)); margin: 24px auto 36px; }}
    .topbar {{
      display: flex; justify-content: space-between; align-items: center; gap: 16px;
      margin-bottom: 16px; color: var(--muted);
      font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.16em;
    }}
    .topbar a {{ color: inherit; text-decoration: none; }}
    .hero {{ margin-bottom: 18px; }}
    .panel {{ border: 1px solid var(--line); border-radius: 24px; background: var(--panel); }}
    .hero-card {{
      padding: 26px 28px 22px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0)),
        radial-gradient(circle at 100% 0%, rgba(139,92,246,0.13), transparent 30%),
        var(--panel);
    }}
    .hero-top {{
      display: flex; justify-content: space-between; align-items: end; gap: 20px;
      padding-bottom: 18px; border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    .hero-meta {{
      display: flex; align-items: center; gap: 12px; margin-top: 12px;
      color: var(--muted); font-size: 13px;
    }}
    .dot-live {{
      width: 8px; height: 8px; border-radius: 999px;
      background: var(--accent-2); box-shadow: 0 0 18px rgba(16,185,129,0.55);
    }}
    h1 {{ margin: 0; font-size: clamp(34px, 5vw, 48px); line-height: 1.02; letter-spacing: -0.05em; }}
    .tz-chip {{
      padding: 8px 14px; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px;
      background: rgba(255,255,255,0.02); color: #8ea3c7;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace; font-size: 12px;
    }}
    .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 18px; }}
    .kpi {{
      padding: 18px 18px 16px; min-height: 168px;
      background: linear-gradient(180deg, rgba(255,255,255,0.015), rgba(255,255,255,0)), var(--panel-2);
    }}
    .kpi-head {{ display: flex; justify-content: space-between; align-items: start; gap: 10px; }}
    .kpi-title {{
      color: rgba(216, 223, 236, 0.54);
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.16em;
    }}
    .kpi-time {{ margin-top: 6px; color: #96a0b5; font-size: 13px; line-height: 1.35; }}
    .delta {{
      display: inline-flex; align-items: center; justify-content: center;
      min-width: 56px; padding: 5px 10px; border-radius: 999px;
      background: rgba(16, 185, 129, 0.14); color: #5ee9b5;
      font-size: 12px; font-weight: 800;
    }}
    .delta--neg {{ background: rgba(239, 68, 68, 0.14); color: #fca5a5; }}
    .kpi-value {{
      margin-top: 18px;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 54px; line-height: 1; letter-spacing: -0.05em; font-weight: 800;
    }}
    .spark {{ height: 38px; margin-top: auto; padding-top: 12px; opacity: 0.9; }}
    .spark svg {{ width: 100%; height: 100%; display: block; }}
    .chart-panel {{ padding: 24px; }}
    .eyebrow {{
      margin: 0 0 10px; color: var(--muted);
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.18em;
    }}
    .chart-head {{
      display: flex; justify-content: space-between; align-items: end; gap: 16px;
      margin-bottom: 18px;
    }}
    .chart-title {{ margin: 0; font-size: 34px; line-height: 0.98; letter-spacing: -0.04em; }}
    .chart-meta {{
      display: flex; align-items: center; gap: 18px; color: var(--muted); font-size: 14px;
      flex-wrap: wrap; justify-content: flex-end;
    }}
    .scale-toggle {{
      display: inline-flex; padding: 4px; border-radius: 999px;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    }}
    .scale-toggle__btn {{
      border: 0; background: transparent; padding: 6px 12px; border-radius: 999px;
      color: #9ca3af; font-size: 12px; font-weight: 700; text-transform: uppercase;
      cursor: pointer; transition: background 0.15s ease, color 0.15s ease;
    }}
    .scale-toggle__btn.is-active {{ background: rgba(255,255,255,0.94); color: #111827; }}
    .scale-toggle__btn:hover:not(.is-active) {{ color: var(--ink); }}
    /* Видимость вариантов чарта и meta-текста по data-scale. */
    .chart-panel[data-scale="linear"] .chart-variant--log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-variant--linear {{ display: none; }}
    .chart-panel[data-scale="linear"] .chart-meta__log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-meta__linear {{ display: none; }}
    .legend {{
      display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 16px;
      color: var(--muted); font-size: 13px;
    }}
    .dot {{
      display: inline-block; width: 10px; height: 10px; margin-right: 6px;
      border-radius: 999px; vertical-align: middle;
    }}
    .chart-shell {{
      padding: 18px; border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0)), #141820;
      border: 1px solid rgba(255,255,255,0.04);
    }}
    .plot {{ display: grid; grid-template-columns: 94px 1fr; gap: 18px; }}
    .axis {{
      display: flex; flex-direction: column; justify-content: space-between; align-items: end;
      padding: 18px 10px 26px 0; color: #8e97a8; font-size: 13px;
      background: repeating-linear-gradient(0deg, transparent 0 68px, var(--grid) 68px 69px);
    }}
    .weeks {{
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
      padding-top: 18px;
      background: repeating-linear-gradient(0deg, transparent 0 68px, var(--grid) 68px 69px);
    }}
    .week {{
      /* top padding 12px симметричен bottom, чтобы W-лэйбл не лип к верхней рамке карточки. */
      padding: 12px; border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid rgba(255,255,255,0.03);
    }}
    .week.current {{
      background: linear-gradient(180deg, rgba(139,92,246,0.10), rgba(139,92,246,0.03));
      border-color: rgba(139,92,246,0.22);
      box-shadow: inset 0 0 0 1px rgba(139,92,246,0.06);
    }}
    .week-head {{
      display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
      margin-bottom: 12px; color: var(--muted);
      font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.14em;
    }}
    .week-total {{
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 12px; font-weight: 700; color: var(--ink);
      text-transform: none; letter-spacing: -0.01em;
    }}
    .week.current .week-total {{ color: var(--accent); }}
    .bars {{ display: flex; gap: 7px; height: 260px; }}
    .bar-cell {{
      /* Обёртка вокруг одного бара. Даёт position:relative для абсолютного
         позиционирования threshold-линии; сам .bar теперь flex-child .bar-cell,
         а не .bars — иначе threshold не привязать к ширине одного дня.
         .bars использует align-items по умолчанию (stretch) — это растягивает
         .bar-cell на всю высоту 260px, без чего height:N% на .bar уехал бы в 0. */
      position: relative; flex: 1; min-width: 0; min-height: 6px;
      display: flex; align-items: end;
    }}
    .bar {{
      width: 100%; border-radius: 10px 10px 0 0; min-height: 6px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
    }}
    .bar.history {{ background: var(--history); }}
    .bar.accent  {{ background: linear-gradient(180deg, #a78bfa, #8b5cf6); }}
    .bar.future {{
      background: rgba(255,255,255,0.03);
      border: 1px dashed rgba(255,255,255,0.12);
      opacity: 0.7; box-shadow: none;
    }}
    /* Threshold — горизонтальная пунктирная линия поверх текущего дня. Линия
       не «дёргает» лейаут: position:absolute + bottom:X% (X считается в
       _render_weekly_grid той же функцией, что высота бара). z-index 2 — чтобы
       быть над баром. На узких экранах подпись прячется, линия остаётся. */
    .threshold {{
      position: absolute; left: -3px; right: -3px;
      border-top: 2px dashed #fca5a5;
      pointer-events: none; z-index: 2;
    }}
    .threshold-label {{
      position: absolute; left: 100%; top: -10px;
      margin-left: 6px; white-space: nowrap;
      font-family: "JetBrains Mono", "Roboto Mono", Consolas, monospace;
      font-size: 10px; font-weight: 700;
      color: #fca5a5; text-transform: none; letter-spacing: 0;
    }}
    .days {{
      display: grid; grid-template-columns: repeat(7, 1fr); gap: 7px;
      margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--history-2);
      color: var(--muted); font-size: 12px; text-align: center;
    }}
    @media (max-width: 980px) {{
      .kpis, .weeks {{ grid-template-columns: 1fr; }}
      .chart-head, .hero-top {{ flex-direction: column; align-items: flex-start; }}
      .plot {{ grid-template-columns: 1fr; }}
      .axis {{ display: none; }}
      /* На узком лейауте подпись threshold'а прячем, линия остаётся. */
      .threshold-label {{ display: none; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <span>Token Dashboard</span>
      <span>Обновлено: {today_label}</span>
    </div>
    <section class="hero">
      <article class="panel hero-card">
        <div class="hero-top">
          <div>
            <h1>Расход токенов runtime</h1>
            <div class="hero-meta">
              <span class="dot-live"></span>
              <span>Обновление каждые 5 минут · Input + Output</span>
            </div>
          </div>
          <div class="tz-chip">Europe/Moscow (UTC+3)</div>
        </div>
        <section class="kpis">
          <article class="panel kpi">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Текущий момент</div>
                <div class="kpi-time">{cur_time}</div>
              </div>
              {delta1}
            </div>
            <div class="kpi-value">{fmt_tokens(current_hour_tokens)}</div>
            <div class="spark">{spark1}</div>
          </article>
          <article class="panel kpi">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Сегодня</div>
                <div class="kpi-time">{today_time}</div>
              </div>
              {delta2}
            </div>
            <div class="kpi-value">{fmt_tokens(today_tokens)}</div>
            <div class="spark">{spark2}</div>
          </article>
          <article class="panel kpi">
            <div class="kpi-head">
              <div>
                <div class="kpi-title">Рабочее время</div>
                <div class="kpi-time">{window_time}</div>
              </div>
              {delta3}
            </div>
            <div class="kpi-value">{fmt_tokens(window_total)}</div>
            <div class="spark">{spark3}</div>
          </article>
        </section>
      </article>
    </section>
    <section class="panel chart-panel" id="chart-scale" data-scale="log">
      <div class="chart-head">
        <div>
          <p class="eyebrow">Weekly Compare</p>
          <h2 class="chart-title">Последние 4 недели, Пн–Вс</h2>
        </div>
        <div class="chart-meta">
          <span class="chart-meta__variant chart-meta__linear">
            Шкала: 0 … {fmt_int(y_max)} токенов ·
            Текущая неделя: <strong>{current_week_label}</strong>
          </span>
          <span class="chart-meta__variant chart-meta__log">
            Шкала: log, {log_meta_range} токенов ·
            Текущая неделя: <strong>{current_week_label}</strong>
          </span>
          <div class="scale-toggle" role="tablist" aria-label="Шкала weekly chart">
            <button type="button" class="scale-toggle__btn" data-scale="linear" role="tab">Линейная</button>
            <button type="button" class="scale-toggle__btn is-active" data-scale="log" role="tab" aria-selected="true">Log</button>
          </div>
        </div>
      </div>
      <div class="legend">
        <span><i class="dot" style="background:rgba(255,255,255,0.16)"></i>W прошлые · history</span>
        <span><i class="dot" style="background:var(--accent)"></i>{current_week_label} · current</span>
        <span><i class="dot" style="background:transparent;border-top:2px dashed #ef4444;height:0;width:14px;border-radius:0;vertical-align:middle"></i>порог недели ({fmt_int(WEEKLY_CAP_TOKENS)} токенов)</span>
      </div>
      <div class="chart-shell">
        <div class="plot chart-variant chart-variant--linear">
          <div class="axis">{axis_linear}</div>
          <div class="weeks">{linear_grid}</div>
        </div>
        <div class="plot chart-variant chart-variant--log">
          <div class="axis">{axis_log}</div>
          <div class="weeks">{log_grid}</div>
        </div>
      </div>
    </section>
  </main>
  <script>
    // Linear/log toggle для weekly chart.
    // Приоритет источников состояния (от сильного к слабому):
    //   1. ?scale=log|linear в URL — для шаринга ссылок.
    //   2. localStorage[tokenDashboardScale] — переживает ребилд dashboard.html.
    //   3. 'log' по умолчанию (как в concept-ops, активна «Log»).
    (function () {{
      var STORAGE_KEY = 'tokenDashboardScale';
      var root = document.getElementById('chart-scale');
      if (!root) return;
      function readStored() {{
        try {{ return localStorage.getItem(STORAGE_KEY); }}
        catch (e) {{ return null; }}
      }}
      function writeStored(scale) {{
        try {{ localStorage.setItem(STORAGE_KEY, scale); }}
        catch (e) {{ /* silent — URL всё ещё работает */ }}
      }}
      function normalize(s) {{ return (s === 'log' || s === 'linear') ? s : null; }}

      var urlScale = normalize(new URLSearchParams(window.location.search).get('scale'));
      var storedScale = normalize(readStored());
      var initial = urlScale || storedScale || 'log';
      if (urlScale) writeStored(urlScale);

      apply(initial, false);
      root.querySelectorAll('.scale-toggle__btn').forEach(function (btn) {{
        btn.addEventListener('click', function () {{ apply(btn.dataset.scale, true); }});
      }});
      function apply(scale, persist) {{
        root.dataset.scale = scale;
        root.querySelectorAll('.scale-toggle__btn').forEach(function (b) {{
          var active = b.dataset.scale === scale;
          b.classList.toggle('is-active', active);
          b.setAttribute('aria-selected', active ? 'true' : 'false');
        }});
        if (persist) {{
          var url = new URL(window.location);
          if (scale === 'linear') url.searchParams.delete('scale');
          else url.searchParams.set('scale', scale);
          history.replaceState(null, '', url);
          writeStored(scale);
        }}
      }}
    }})();
  </script>
</body>
</html>
"""


# ---- main ------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # Прогрессия логов в UTF-8 (Windows-консоль).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    log = (lambda *a, **kw: print(*a, **kw)) if not args.quiet else (lambda *a, **kw: None)

    log(f"[build] db  = {args.db}")
    log(f"[build] out = {args.out}")

    today = datetime.now(MSK).date()
    iso = today.isocalendar()
    log(f"[build] today (MSK) = {today}  ISO W-{iso[1]}  weekday={iso[2]}")

    # 4-недельное окно: с понедельника 3 недели назад от текущего.
    current_monday = today - timedelta(days=iso[2] - 1)
    since = current_monday - timedelta(weeks=WEEK_COUNT - 1)
    log(f"[build] since = {since} (Monday of W-{since.isocalendar()[1]})")

    with open_db(args.db) as con:
        hourly = aggregate_by_hour(con, since)

    log(f"[build] aggregated {len(hourly)} (date,hour) buckets")

    now_msk = datetime.now(MSK)
    current_hour_tokens = compute_current_hour(hourly, today)
    today_tokens = compute_today(hourly, now_msk)
    window_total, window_entries, window_label = compute_current_window(hourly, now_msk)
    window_wraps = current_window(now_msk)["wraps"]
    weeks = compute_weekly(hourly, today)

    # Sparklines (3 KPI) + предыдущие периоды (для дельты)
    spark_current = compute_sparkline_current(hourly, now_msk)
    spark_today = compute_sparkline_today(hourly, now_msk)
    spark_window = compute_sparkline_window(window_entries)
    prev_hour = compute_prev_hour(hourly, now_msk)
    prev_day = compute_prev_day_today(hourly, now_msk)
    prev_window = compute_prev_window_total(hourly, now_msk)

    # Шкалы weekly chart
    y_max = _y_max_for(weeks)
    log_info = _y_ticks_for_log(weeks)

    # Порог расхода на сегодня (weekly cap threshold). Считаем только если
    # текущая неделя действительно последняя в окне (она всегда последняя по
    # логике compute_weekly) и для today есть ненулевая запись. Если записи
    # ещё нет — today_spent=0, threshold=cap/days_left (нормальный кейс для
    # самого начала дня).
    current_week = weeks[-1]
    today_idx = now_msk.weekday()  # 0=Пн..6=Вс
    today_spent = current_week.days[today_idx] or 0
    days_left = 8 - now_msk.isoweekday()  # Пн=7, Вс=1
    weekly_threshold = compute_weekly_threshold(
        WEEKLY_CAP_TOKENS, today_spent, days_left
    )
    log(
        f"[build] weekly_cap={WEEKLY_CAP_TOKENS}  "
        f"today_spent={today_spent}  days_left={days_left}  "
        f"threshold={weekly_threshold}"
    )

    log(f"[build] current_hour ({today}, {now_msk.hour:02d}h) = {current_hour_tokens}")
    log(f"[build] today       (00:00–{now_msk.hour:02d}:59) = {today_tokens}")
    log(f"[build] active_window = {window_label}, total = {window_total}")
    for h, v, d in window_entries:
        log(f"[build]   {d.isoformat()} {h:02d}:00-{h:02d}:59 = {v}")
    for w in weeks:
        days_repr = ",".join(
            fmt_int(v) if v is not None else "—" for v in w.days
        )
        log(f"[build]   {w.label} ({'current' if w.is_current else 'past  '})  [{days_repr}]")

    html = render_html(
        current_hour_tokens=current_hour_tokens,
        current_hour_delta=fmt_delta_pct(current_hour_tokens, prev_hour),
        current_hour_sparkline=spark_current,
        today_tokens=today_tokens,
        today_delta=fmt_delta_pct(today_tokens, prev_day),
        today_sparkline=spark_today,
        window_total=window_total,
        window_delta=fmt_delta_pct(window_total, prev_window),
        window_sparkline=spark_window,
        window_label=window_label,
        window_wraps=window_wraps,
        weeks=weeks,
        y_max=y_max,
        log_info=log_info,
        weekly_threshold=weekly_threshold,
    )

    if args.no_write:
        sys.stdout.write(html)
        log("\n[build] --no-write: stdout-only, файл не тронут")
    else:
        args.out.write_text(html, encoding="utf-8")
        log(f"[build] wrote {args.out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
