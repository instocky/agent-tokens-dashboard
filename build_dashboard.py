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

# 5-часовые слоты, по которым бьётся "текущее окно".
# 4 дневных слота (по 5 часов) + 1 ночной (4 часа, переход через полночь).
# PRD §6.3 — слот выбирается по now.hour, а не фиксирован.
# Ночной слот асимметричен (4h вместо 5h) — это сознательный компромисс:
# честное "23:00-02:59" важнее, чем добивать до 5 часов через 22:00 или 03:00.
WINDOWS: list[dict] = [
    {"name": "morning",   "hours": [3, 4, 5, 6, 7],     "label": "03:00–07:59", "wraps": False},
    {"name": "midday",    "hours": [8, 9, 10, 11, 12],  "label": "08:00–12:59", "wraps": False},
    {"name": "afternoon", "hours": [13, 14, 15, 16, 17], "label": "13:00–17:59", "wraps": False},
    {"name": "evening",   "hours": [18, 19, 20, 21, 22], "label": "18:00–22:59", "wraps": False},
    {"name": "night",     "hours": [23, 0, 1, 2],       "label": "23:00–02:59", "wraps": True},
]
NIGHT_SLOT = WINDOWS[4]
WEEK_COUNT: int = 4                                # PRD §6.4
WEEKDAY_LABELS: tuple[str, ...] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Палитра по индексу недели (0 = самая старая, WEEK_COUNT-1 = текущая).
# Дублирует дизайн-токены из dashboard-chart-prototype.html.
WEEK_PALETTE: list[dict[str, str]] = [
    {  # W-N (oldest)
        "solid": "#d95d39", "solid_tw": "fill-orange-500", "solid_bg": "bg-orange-500",
        "bright": "#f6ad55", "bright_tw": "fill-orange-300",
        "stroke": "stroke-orange-500", "stroke_bright": "stroke-orange-300",
    },
    {  # W-N+1
        "solid": "#257179", "solid_tw": "fill-teal-600", "solid_bg": "bg-teal-600",
        "bright": "#4fd1c5", "bright_tw": "fill-teal-400",
        "stroke": "stroke-teal-600", "stroke_bright": "stroke-teal-400",
    },
    {  # W-N+2
        "solid": "#d9a441", "solid_tw": "fill-amber-500", "solid_bg": "bg-amber-500",
        "bright": "#f6d365", "bright_tw": "fill-amber-300",
        "stroke": "stroke-amber-500", "stroke_bright": "stroke-amber-300",
    },
    {  # W-N+3 (current)
        "solid": "#4f5d75", "solid_tw": "fill-slate-600", "solid_bg": "bg-slate-600",
        "bright": "#94a3b8", "bright_tw": "fill-slate-400",
        "stroke": "stroke-slate-600", "stroke_bright": "stroke-slate-400",
    },
]


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


def render_html(
    current_hour_tokens: int,
    today_tokens: int,
    window_total: int,
    window_entries: list[tuple[int, int, date]],
    window_label: str,
    window_wraps: bool,
    weeks: list[Week],
) -> str:
    now_msk = datetime.now(MSK)
    today_label = now_msk.strftime("%Y-%m-%d %H:%M MSK")
    week_labels = [w.label for w in weeks]
    window_max = max((v for _, v, _ in window_entries), default=0)
    if window_wraps:
        window_note = (
            f"Сумма за {len(window_entries)} ночных часа (UTC+3), "
            f"с 23:00 вчера по 02:59 сегодня"
        )
    else:
        window_note = (
            f"Сумма за {len(window_entries)} часов (UTC+3), сегодня"
        )
    y_max = _y_max_for(weeks)
    log_info = _y_ticks_for_log(weeks)

    # Серия для JS-варианта (line / hybrid) — оставлено в HTML для совместимости
    # с prototype, но дефолтный рендер — bars. PRD §7.3.
    series_json = json.dumps(
        [
            {
                "name": w.label,
                "color": WEEK_PALETTE[i]["solid"],
                "is_current": w.is_current,
                "values": [v if v is not None else 0 for v in w.days],
                "nulls": [v is None for v in w.days],
            }
            for i, w in enumerate(weeks)
        ],
        ensure_ascii=False,
    )
    weekday_labels_json = json.dumps(list(WEEKDAY_LABELS), ensure_ascii=False)
    palette_json = json.dumps(WEEK_PALETTE, ensure_ascii=False)
    y_max_json = json.dumps(y_max)

    # Мини-бары по часам активного слота (SVG).
    window_bars_svg = _render_window_bars(window_entries, window_max)
    # Weekly grouped bars — оба варианта (linear / log), видимость через CSS.
    # Дефолт — linear; log включается кликом, состояние в ?scale=log.
    linear_bars_svg = _render_weekly_bars(weeks, "linear", y_max)
    log_bars_svg = _render_weekly_bars(weeks, "log", log_info) if log_info else ""

    # Meta-текст для log-варианта. None → "log, нет данных".
    if log_info:
        log_meta_range = f"log, {fmt_tokens(int(log_info[0]))} … {fmt_tokens(int(log_info[1]))}"
    else:
        log_meta_range = "log, нет данных"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Token Dashboard — {today_label}</title>
  <style>
    :root {{
      --bg: #f5efe2;
      --panel: rgba(255, 251, 245, 0.9);
      --ink: #1e1d1a;
      --muted: #6f6a60;
      --line: #d8cdb8;
      --shadow: 0 22px 50px rgba(54, 40, 18, 0.14);
      --disabled: #d8d0c3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(217, 164, 65, 0.16), transparent 30%),
        radial-gradient(circle at bottom right, rgba(37, 113, 121, 0.18), transparent 28%),
        linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
    }}
    .shell {{ width: min(1180px, calc(100vw - 48px)); margin: 32px auto 60px; }}
    .hero {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 20px;
      margin-bottom: 20px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(111, 106, 96, 0.14);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }}
    .intro {{ padding: 28px 30px; }}
    .eyebrow {{
      margin: 0 0 8px;
      font: 600 12px/1.2 "Trebuchet MS", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--muted);
    }}
    h1 {{ margin: 0; font-size: 42px; line-height: 0.96; font-weight: 700; }}
    .lede {{
      margin: 14px 0 0;
      max-width: 52ch;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.55;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      padding: 20px;
    }}
    .stat {{
      padding: 18px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.55);
      border: 1px solid rgba(111, 106, 96, 0.1);
    }}
    .stat--wide {{ grid-column: span 2; }}
    .stat-label {{
      font: 600 11px/1.2 "Trebuchet MS", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
    }}
    .stat-value {{ margin-top: 8px; font-size: 34px; line-height: 1; }}
    .stat-note {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }}
    .stat-morning {{
      display: grid;
      grid-template-columns: 1fr 1.2fr;
      gap: 16px;
      align-items: center;
    }}
    .stat-window .window-bars {{ width: 100%; height: 66px; }}
    .chart-panel {{ padding: 24px; }}
    .chart-head {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }}
    .chart-title {{ margin: 0; font-size: 28px; line-height: 1.05; }}
    .chart-meta {{
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      justify-content: flex-end;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.4;
    }}
    .chart-meta__variant {{ display: inline; }}
    /* scale-toggle: сегментированный контрол в духе eyebrow-стиля дашборда */
    .scale-toggle {{
      display: inline-flex;
      align-items: center;
      flex-shrink: 0;
      padding: 2px;
      border: 1px solid rgba(111, 106, 96, 0.22);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.55);
    }}
    .scale-toggle__btn {{
      border: 0;
      background: transparent;
      padding: 4px 12px;
      font: 600 11px/1 "Trebuchet MS", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      border-radius: 999px;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;
    }}
    .scale-toggle__btn:hover:not(.is-active) {{ color: var(--ink); }}
    .scale-toggle__btn.is-active {{
      background: var(--ink);
      color: #fbf7ef;
    }}
    /* Видимость вариантов чарта и meta-текста по data-scale на .chart-panel
       (общий предок .chart-meta и #chart). Атрибут data-scale переезжает
       именно на эту секцию, потому что .chart-meta и #chart — сиблинги,
       а не parent/child. */
    .chart-panel[data-scale="linear"] .chart-variant--log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-variant--linear {{ display: none; }}
    .chart-panel[data-scale="linear"] .chart-meta__log {{ display: none; }}
    .chart-panel[data-scale="log"]    .chart-meta__linear {{ display: none; }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 0 0 18px;
      padding: 0;
      list-style: none;
    }}
    .legend li {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font: 600 13px/1.2 "Trebuchet MS", sans-serif;
    }}
    .swatch {{ width: 14px; height: 14px; border-radius: 999px; }}
    #chart {{
      width: 100%;
      min-height: 300px;
      border-radius: 20px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.55), rgba(255,255,255,0.15)),
        repeating-linear-gradient(
          0deg,
          transparent 0 67px,
          rgba(111, 106, 96, 0.08) 67px 68px
        );
      border: 1px solid rgba(111, 106, 96, 0.12);
      padding: 18px;
    }}
    .footer {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      font-family: "Trebuchet MS", sans-serif;
    }}
    @media (max-width: 900px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .summary {{ grid-template-columns: 1fr; }}
      .stat--wide {{ grid-column: span 1; }}
      #chart {{ min-height: 240px; padding: 10px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <article class="panel intro">
        <p class="eyebrow">Token usage</p>
        <h1>Расход токенов runtime</h1>
        <p class="lede">
          Локальный дашборд, пересобирается каждые 5 минут.
          Метрика — input&nbsp;+&nbsp;output токены (кэш, reasoning, cost не учитываются).
          Время — Europe/Moscow (UTC+3).
        </p>
      </article>
      <section class="panel summary">
        <article class="stat">
          <div class="stat-label">Токены / текущий час</div>
          <div class="stat-value">{fmt_tokens(current_hour_tokens)}</div>
          <div class="stat-note">
            Календарный час {now_msk.hour:02d}:00–{now_msk.hour:02d}:59, {today_label[:10]}
          </div>
        </article>
        <article class="stat">
          <div class="stat-label">Токены / сегодня</div>
          <div class="stat-value">{fmt_tokens(today_tokens)}</div>
          <div class="stat-note">
            С начала суток: 00:00–{now_msk.hour:02d}:59, {today_label[:10]}
          </div>
        </article>
        <article class="stat stat--wide stat-window">
          <div>
            <div class="stat-label">Токены в окне {window_label}</div>
            <div class="stat-value">{fmt_tokens(window_total)}</div>
            <div class="stat-note">{window_note}</div>
          </div>
          <svg class="window-bars" viewBox="0 0 240 66" preserveAspectRatio="none" aria-label="Current window breakdown">
            {window_bars_svg}
          </svg>
        </article>
      </section>
    </section>

    <section class="panel chart-panel" id="chart-scale" data-scale="linear">
      <div class="chart-head">
        <div>
          <p class="eyebrow">Weekly compare</p>
          <h2 class="chart-title">Последние 4 недели, Пн–Вс</h2>
        </div>
        <div class="chart-meta">
          <span class="chart-meta__variant chart-meta__linear">
            Шкала: 0 … {fmt_int(y_max)} токенов ·
            Текущая неделя: <strong>{week_labels[-1]}</strong>
          </span>
          <span class="chart-meta__variant chart-meta__log">
            Шкала: {log_meta_range} токенов ·
            Текущая неделя: <strong>{week_labels[-1]}</strong>
          </span>
          <div class="scale-toggle" role="tablist" aria-label="Шкала weekly chart">
            <button type="button" class="scale-toggle__btn is-active"
                    data-scale="linear" role="tab" aria-selected="true">Линейная</button>
            <button type="button" class="scale-toggle__btn"
                    data-scale="log" role="tab" aria-selected="false">Log</button>
          </div>
        </div>
      </div>
      <ul class="legend">
        {"".join(
            f'<li><span class="swatch" style="background:{WEEK_PALETTE[i]["solid"]}"></span>'
            f'<span>{w.label}{" (current)" if w.is_current else ""}</span></li>'
            for i, w in enumerate(weeks)
        )}
      </ul>
      <div id="chart" aria-live="polite">
        <div class="chart-variant chart-variant--linear">
          <svg viewBox="0 0 1040 280" width="100%" height="100%" role="img"
               aria-label="Grouped bars weekly token comparison (linear scale)">
            {linear_bars_svg}
          </svg>
        </div>
        <div class="chart-variant chart-variant--log">
          <svg viewBox="0 0 1040 280" width="100%" height="100%" role="img"
               aria-label="Grouped bars weekly token comparison (log scale)">
            {log_bars_svg}
          </svg>
        </div>
      </div>
      <div class="footer">
        Сгенерировано: {today_label} ·
        Источник: local_runtime_token_usage
        ({fmt_int(sum(int(v) for w in weeks for v in w.days if v is not None))} токенов за 4 недели)
      </div>
    </section>
  </main>
  <script>
    // Linear/log toggle для weekly chart.
    // Приоритет источников состояния (от сильного к слабому):
    //   1. ?scale=log|linear в URL — для шаринга ссылок.
    //   2. localStorage[tokenDashboardScale] — переживает <meta http-equiv="refresh">
    //      каждые 60s и ребилд dashboard.html каждые 5 мин (когда URL квери
    //      стрипается в некоторых браузерах).
    //   3. 'linear' по умолчанию — новые посетители видят честную абсолютную шкалу.
    // localStorage пишется при каждом клике, поэтому и URL, и storage всегда
    // консистентны с последним выбором пользователя.
    (function () {{
      var STORAGE_KEY = 'tokenDashboardScale';
      var root = document.getElementById('chart-scale');
      if (!root) return;
      function readStored() {{
        try {{ return localStorage.getItem(STORAGE_KEY); }}
        catch (e) {{ return null; }}  // private mode / file:// restrictions
      }}
      function writeStored(scale) {{
        try {{ localStorage.setItem(STORAGE_KEY, scale); }}
        catch (e) {{ /* silent — URL всё ещё работает */ }}
      }}
      function normalize(s) {{ return (s === 'log' || s === 'linear') ? s : null; }}

      var urlScale = normalize(new URLSearchParams(window.location.search).get('scale'));
      var storedScale = normalize(readStored());
      var initial = urlScale || storedScale || 'linear';
      if (urlScale) writeStored(urlScale);  // шаринг-ссылка прописывается в storage

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


def _render_window_bars(
    entries: list[tuple[int, int, date]], max_value: int
) -> str:
    """Мини-бары по часам активного слота + лейблы часов под столбцами.

    entries: [(hour, value, date), ...] — порядок как в окне (для ночного
    23 идёт первым, потом 0/1/2; для дневного — по возрастанию).
    Кол-во баров адаптивно: 5 для дневных слотов, 4 для ночного.

    Layout viewBox (240x66):
      - y 0..52:   bar zone  (бары растут снизу вверх от y=52)
      - y 52..66:  label zone (час-цифра baseline y=62, font-size 9)
    Раньше лейблы сидели на y=55 в 56px viewBox и рисовались ВНУТРИ/ПОВЕРХ
    столбцов — на рендере их не было видно. Теперь — отдельная зона под барами.
    """
    if max_value <= 0:
        max_value = 1
    width, bar_h, label_h = 240, 52, 14
    total_h = bar_h + label_h  # 66
    margin_l, margin_r, gap = 8, 8, 6
    n = len(entries)
    bar_w = (width - margin_l - margin_r - gap * (n - 1)) / n
    bars = []
    for i, (h, v, d) in enumerate(entries):
        x = margin_l + i * (bar_w + gap)
        # 4px top padding для максимального бара — было 14 в старом layout.
        h_px = max(2.0, (v / max_value) * (bar_h - 4))
        y = bar_h - h_px
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h_px:.1f}" rx="3" '
            f'fill="#257179">'
            f'<title>{d.isoformat()} {h:02d}:00–{h:02d}:59: {fmt_int(v)}</title>'
            f'</rect>'
        )
        # Baseline y = bar_h + 10: 2px gap от бара + 8px top-padding внутри label_h.
        bars.append(
            f'<text x="{x + bar_w/2:.1f}" y="{bar_h + 10}" text-anchor="middle" '
            f'font-size="9" fill="#6f6a60">{h:02d}</text>'
        )
    return "".join(bars)


def _render_weekly_bars(
    weeks: list[Week],
    scale: str,
    y_info,
) -> str:
    """Grouped bars: 4 группы по 7 дней, future/no-data → dashed placeholder.

    scale: 'linear' | 'log'.
    y_info:
        - linear: int (y_max), тики фиксированные [0, 25%, 50%, 75%, 100%].
        - log:    (y_min: float, y_max: float, ticks: list[int]) — степени 10.
                  0-value день → 2px floor bar (без log(0)).
    """
    width, height = 1040, 280
    margin = {"top": 18, "right": 20, "bottom": 60, "left": 92}
    inner_w = width - margin["left"] - margin["right"]
    inner_h = height - margin["top"] - margin["bottom"]
    # Layout (в единицах day-slot): 1 слева + 4*(7+2 между) + 1 справа = 36.
    n_weeks = len(weeks)
    n_days = 7
    left_pad = 1          # 1 day-width padding слева
    inter_week_gap = 2    # 2 day-width gap между неделями
    right_pad = 1         # 1 day-width padding справа
    slot_total = left_pad + n_weeks * n_days + (n_weeks - 1) * inter_week_gap + right_pad
    day_slot = inner_w / slot_total
    week_group_w = (n_days + inter_week_gap) * day_slot
    bar_w = day_slot - 3
    day_area_w = day_slot * n_days

    if scale == "log":
        y_min, y_max, y_ticks = y_info
        log_min = math.log10(y_min)
        log_max = math.log10(y_max)
        log_span = log_max - log_min

        def _to_y(value: int) -> float:
            """Y-координата верха бара (в px). 0 → пол (не log(0))."""
            if value <= 0:
                return margin["top"] + inner_h
            frac = (math.log10(value) - log_min) / log_span
            return margin["top"] + inner_h - frac * inner_h

        def _to_h(value: int) -> float:
            """Высота бара (px). 0 → 2px floor bar."""
            if value <= 0:
                return 2.0
            frac = (math.log10(value) - log_min) / log_span
            return frac * inner_h
    else:
        y_max = y_info
        y_ticks = [0, y_max // 4, y_max // 2, 3 * y_max // 4, y_max]

        def _to_y(value: int) -> float:
            return margin["top"] + inner_h - (value / y_max) * inner_h

        def _to_h(value: int) -> float:
            return (value / y_max) * inner_h

    # Grid lines + tick labels.
    grid_parts = []
    for tick in y_ticks:
        y = _to_y(tick)
        grid_parts.append(
            f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" y2="{y:.1f}" '
            f'stroke="rgba(111,106,96,0.14)" />'
        )
        # Linear — fmt_int (26 000 000). Log — компактный "100K" / "10M".
        label = fmt_int(tick) if scale == "linear" else fmt_tokens(tick)
        grid_parts.append(
            f'<text x="{margin["left"] - 12}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#6f6a60">{label}</text>'
        )
    grid = "".join(grid_parts)

    groups = []
    for w_idx, week in enumerate(weeks):
        x0 = margin["left"] + left_pad * day_slot + w_idx * week_group_w
        palette = WEEK_PALETTE[w_idx]
        bar_parts = []
        for d_idx in range(7):
            value = week.days[d_idx]
            x = x0 + d_idx * day_slot
            is_weekend = d_idx in (5, 6)
            if value is None:
                # disabled: dashed placeholder bar
                bar_parts.append(
                    f'<rect x="{x:.1f}" y="{margin["top"] + 10}" width="{bar_w:.1f}" '
                    f'height="{inner_h - 10:.1f}" rx="7" '
                    f'fill="rgba(216,208,195,0.35)" '
                    f'stroke="rgba(216,208,195,0.7)" stroke-dasharray="5 5">'
                    f'<title>{week.label}, {WEEKDAY_LABELS[d_idx]}: нет данных</title>'
                    f'</rect>'
                )
                bar_parts.append(
                    f'<text x="{x + bar_w/2:.1f}" y="{height - 38}" text-anchor="middle" '
                    f'font-size="10.5" fill="#6f6a60">{WEEKDAY_LABELS[d_idx]}</text>'
                )
                continue
            h_px = _to_h(value)
            y = _to_y(value)
            if value == 0 and scale == "log":
                # Floor bar: день есть, но 0 токенов. Тонкий, в disabled-цвете.
                bar_parts.append(
                    f'<rect x="{x:.1f}" y="{y - h_px:.1f}" width="{bar_w:.1f}" '
                    f'height="{h_px:.1f}" rx="2" '
                    f'fill="rgba(216,208,195,0.7)">'
                    f'<title>{week.label}, {WEEKDAY_LABELS[d_idx]}: 0 (log floor)</title>'
                    f'</rect>'
                )
            else:
                fill_class = palette["bright_tw"] if is_weekend else palette["solid_tw"]
                bar_parts.append(
                    f'<rect class="{fill_class}" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                    f'height="{h_px:.1f}" rx="7" opacity="{"1" if week.is_current else "0.88"}">'
                    f'<title>{week.label}, {WEEKDAY_LABELS[d_idx]}: {fmt_int(value)}</title>'
                    f'</rect>'
                )
            bar_parts.append(
                f'<text x="{x + bar_w/2:.1f}" y="{height - 38}" text-anchor="middle" '
                f'font-size="10.5" fill="#6f6a60">{WEEKDAY_LABELS[d_idx]}</text>'
            )
        bars = "".join(bar_parts)
        highlight = ""
        if week.is_current:
            highlight = (
                f'<rect x="{x0 - 4:.1f}" y="{margin["top"] - 6}" '
                f'width="{day_area_w + 8:.1f}" height="{inner_h + 28:.1f}" rx="16" '
                f'fill="rgba(79,93,117,0.06)" stroke="rgba(79,93,117,0.16)" />'
            )
        groups.append(
            f'<g>{highlight}{bars}'
            f'<text x="{x0 + day_area_w/2:.1f}" y="{height - 16}" text-anchor="middle" '
            f'font-size="13" font-weight="700" fill="#1e1d1a">{week.label}</text></g>'
        )
    return f'<rect width="0" height="0" fill="none" />{grid}{"".join(groups)}'


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
        current_hour_tokens,
        today_tokens,
        window_total,
        window_entries,
        window_label,
        window_wraps,
        weeks,
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
