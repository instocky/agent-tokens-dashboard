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
    values = [d for w in weeks for d in w.days if d is not None]
    if not values:
        return 1000
    # Округляем max до ближайших 100K, минимум 200K.
    raw = max(values)
    if raw < 200_000:
        return 200_000
    step = 200_000 if raw <= 2_000_000 else 1_000_000
    return ((raw + step - 1) // step) * step


def render_html(
    current_hour_tokens: int,
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
    # Weekly grouped bars (дефолтный вариант PRD).
    weekly_bars_svg = _render_weekly_bars(weeks, y_max)

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
    .stat-window .window-bars {{ width: 100%; height: 56px; }}
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
      color: var(--muted);
      font-size: 14px;
      line-height: 1.4;
    }}
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
        <article class="stat stat--wide stat-window">
          <div>
            <div class="stat-label">Токены в окне {window_label}</div>
            <div class="stat-value">{fmt_tokens(window_total)}</div>
            <div class="stat-note">{window_note}</div>
          </div>
          <svg class="window-bars" viewBox="0 0 240 56" preserveAspectRatio="none" aria-label="Current window breakdown">
            {window_bars_svg}
          </svg>
        </article>
      </section>
    </section>

    <section class="panel chart-panel">
      <div class="chart-head">
        <div>
          <p class="eyebrow">Weekly compare</p>
          <h2 class="chart-title">Последние 4 недели, Пн–Вс</h2>
        </div>
        <div class="chart-meta">
          Шкала: 0 … {fmt_int(y_max)} токенов ·
          Текущая неделя: <strong>{week_labels[-1]}</strong>
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
        <svg viewBox="0 0 1040 280" width="100%" height="100%" role="img"
             aria-label="Grouped bars weekly token comparison">
          {weekly_bars_svg}
        </svg>
      </div>
      <div class="footer">
        Сгенерировано: {today_label} ·
        Источник: local_runtime_token_usage
        ({fmt_int(sum(int(v) for w in weeks for v in w.days if v is not None))} токенов за 4 недели)
      </div>
    </section>
  </main>
</body>
</html>
"""


def _render_window_bars(
    entries: list[tuple[int, int, date]], max_value: int
) -> str:
    """Мини-бары по часам активного слота в одну строку.

    entries: [(hour, value, date), ...] — порядок как в окне (для ночного
    23 идёт первым, потом 0/1/2; для дневного — по возрастанию).
    Кол-во баров адаптивно: 5 для дневных слотов, 4 для ночного.
    """
    if max_value <= 0:
        max_value = 1
    width, height = 240, 56
    margin_l, margin_r, gap = 8, 8, 6
    n = len(entries)
    bar_w = (width - margin_l - margin_r - gap * (n - 1)) / n
    bars = []
    for i, (h, v, d) in enumerate(entries):
        x = margin_l + i * (bar_w + gap)
        h_px = max(2.0, (v / max_value) * (height - 14))
        y = height - h_px
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h_px:.1f}" rx="3" '
            f'fill="#257179">'
            f'<title>{d.isoformat()} {h:02d}:00–{h:02d}:59: {fmt_int(v)}</title>'
            f'</rect>'
        )
        bars.append(
            f'<text x="{x + bar_w/2:.1f}" y="{height - 1}" text-anchor="middle" '
            f'font-size="9" fill="#6f6a60">{h:02d}</text>'
        )
    return "".join(bars)


def _render_weekly_bars(weeks: list[Week], max_value: int) -> str:
    """Grouped bars: 4 группы по 7 дней, future/no-data → dashed placeholder."""
    width, height = 1040, 280
    margin = {"top": 18, "right": 20, "bottom": 60, "left": 92}
    inner_w = width - margin["left"] - margin["right"]
    inner_h = height - margin["top"] - margin["bottom"]
    week_group_w = inner_w / len(weeks)
    day_slot = week_group_w / 10
    bar_w = day_slot - 3
    day_area_w = day_slot * 7

    # Шкала: 5 тиков 0, 25%, 50%, 75%, 100% от max.
    y_ticks = [0, max_value // 4, max_value // 2, 3 * max_value // 4, max_value]
    grid_parts = []
    for tick in y_ticks:
        y = margin["top"] + inner_h - (tick / max_value) * inner_h
        grid_parts.append(
            f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" y2="{y:.1f}" '
            f'stroke="rgba(111,106,96,0.14)" />'
        )
        grid_parts.append(
            f'<text x="{margin["left"] - 12}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#6f6a60">{fmt_int(tick)}</text>'
        )
    grid = "".join(grid_parts)

    groups = []
    for w_idx, week in enumerate(weeks):
        x0 = margin["left"] + w_idx * week_group_w
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
                # день-лейбл всё равно под ним
                bar_parts.append(
                    f'<text x="{x + bar_w/2:.1f}" y="{height - 38}" text-anchor="middle" '
                    f'font-size="10.5" fill="#6f6a60">{WEEKDAY_LABELS[d_idx]}</text>'
                )
                continue
            h_px = (value / max_value) * inner_h
            y = margin["top"] + inner_h - h_px
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
    window_total, window_entries, window_label = compute_current_window(hourly, now_msk)
    window_wraps = current_window(now_msk)["wraps"]
    weeks = compute_weekly(hourly, today)

    log(f"[build] current_hour ({today}, {now_msk.hour:02d}h) = {current_hour_tokens}")
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
