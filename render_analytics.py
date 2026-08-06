"""render_analytics.py — render(snapshot) -> HTML-строка (ADR-002 §2.6).

Чистая render-сторона для второго вида (`analytics.html` — calendar heatmap
4 недели × 7 дней). Принимает snapshot (dict по контракту §2.4 ADR-001 +
daily-секция по §2.4 ADR-002), собранный `analytics.build_snapshot(db_path,
now_msk)`. Никогда не открывает SQLite и не зовёт compute-функции напрямую —
все данные приходят через snapshot.

Импортирует из analytics ТОЛЬКО константы, доменные типы и форматирующие
хелперы (`fmt_*`, `DailyBar`, `WEEKDAY_LABELS`) — направление импорта
однонаправленное render → analytics (правило 8 §2.2 ADR-001). Compute, доступ
к БД и `pill_level` живут в analytics; значение `burn_today` приходит в
snapshot уже посчитанным.

Запуск напрямую не предполагается — это модуль, импортируемый из
`build_analytics.py` (entry) и тестов.
"""
from __future__ import annotations

from datetime import datetime

# Импорты из analytics — допустимые по §2.2 rule 8 ADR-001 (константы, типы,
# форматтеры). НЕ импортируем compute-функции, DB-доступ и `pill_level`.
from analytics import (  # noqa: E402
    WEEKDAY_LABELS,
    DailyBar,
    fmt_tokens,
)


# ---- helpers --------------------------------------------------------------


def _week_columns(bars: list[DailyBar], week_count: int) -> list[list[DailyBar]]:
    """Разбить 28-дневный плоский список на 4 колонки × 7 строк.

    bars[0..6] = W-3 (oldest), bars[7..13] = W-2, …, bars[21..27] = W-0.
    Внутри колонки порядок Пн..Вс (oldest-first внутри недели). Возвращаем
    список колонок (от W-3 до W-0) — каждая колонка = 7 баров.
    """
    cols: list[list[DailyBar]] = []
    for w in range(week_count):
        cols.append(bars[w * 7:(w + 1) * 7])
    return cols


def _week_label(bar: DailyBar) -> str:
    """Лейбл колонки недели: 'W-32' (по ISO week первой ячейки колонки)."""
    return f"W-{bar.iso_week}"


def _burn_label(level: str, avg: int | None) -> str:
    """Текст burn-строки: 'burn сегодня: ok / warn / over / none'."""
    if level == "none" or avg is None:
        return "burn сегодня: —"
    return f"burn сегодня: <strong>{level}</strong> (vs 7d avg {fmt_tokens(avg)})"


# ---- per-cell render ------------------------------------------------------


def _render_daily_cell(bar: DailyBar, is_current_week: bool) -> str:
    """Одна ячейка heatmap'а.

    Классы (порядок важен — для читаемости CSS):
      - `.daily-cell`                          — базовый
      - `.daily-cell.state-{state}`            — active|empty|current|future
      - `.daily-cell.intensity-{L1..L4}`       — только для active/current с value>0
      - `.daily-cell.current-week`             — для всей W-0 колонки (outline)

    Tooltip (title=) — DD.MM.YYYY, weekday + значение + уровень. Это статичная
    подсказка; никакого JS / popup'ов (ADR-002 §4).

    value=fmt_tokens(bar.value) для active/current с value>0; "—" для
    empty/future; для current с value=0 — "0" (явный ноль, не тире).
    """
    state = bar.state
    if state in ("active", "current") and bar.value is not None:
        if bar.value == 0:
            value_text = "0"
        else:
            value_text = fmt_tokens(bar.value)
        intensity_cls = (
            f" intensity-{bar.intensity}" if bar.intensity else ""
        )
    else:
        value_text = "—"
        intensity_cls = ""

    cw_cls = " current-week" if is_current_week else ""

    date_label = bar.date.strftime("%d.%m")
    weekday_label = WEEKDAY_LABELS[bar.weekday]
    year_label = bar.date.strftime("%Y")

    if state in ("active", "current") and bar.value is not None and bar.value > 0:
        intensity_label = bar.intensity or "—"
        title = (
            f"{date_label}.{year_label}, {weekday_label} — "
            f"{fmt_tokens(bar.value)} ({intensity_label})"
        )
    elif state == "current" and bar.value == 0:
        title = f"{date_label}.{year_label}, {weekday_label} — сегодня (0)"
    else:
        # empty / future
        title = f"{date_label}.{year_label}, {weekday_label} — {state}"

    return (
        f'<div class="daily-cell state-{state}{intensity_cls}{cw_cls}" '
        f'title="{title}">'
        f'<div class="daily-value">{value_text}</div>'
        f'<div class="daily-date">{date_label}</div>'
        f"</div>"
    )


# ---- per-row / per-grid render --------------------------------------------


def _render_daily_grid(bars: list[DailyBar], week_count: int) -> str:
    """Сетка 7×4: 4 колонки (W-3..W-0), 7 строк (Пн..Вс).

    Layout: CSS grid с 4 столбцами одинаковой ширины. Каждая колонка
    содержит 7 ячеек (Пн→Вс). Колонка текущей недели (W-0) получает
    outline через `.current-week` на каждой ячейке + `.col.current-week`
    на самой колонке (для двойной подсветки outline'а).
    """
    cols = _week_columns(bars, week_count)
    html_cols: list[str] = []
    for w_idx, col in enumerate(cols):
        is_current = (w_idx == week_count - 1)
        cw_col_cls = " current-week" if is_current else ""
        label = _week_label(col[0])
        cells = "".join(
            _render_daily_cell(b, is_current_week=is_current) for b in col
        )
        html_cols.append(
            f'<div class="daily-col{cw_col_cls}">'
            f'<div class="daily-col-head">{label}</div>'
            f'{cells}'
            f"</div>"
        )
    return f'<div class="daily-grid">{"".join(html_cols)}</div>'


# ---- card / page ---------------------------------------------------------


def _render_daily_card(snapshot: dict) -> str:
    """Один card с заголовком, 7×4 heatmap'ом и meta-строкой."""
    daily = snapshot["daily"]
    since = daily["since"]
    burn_today = daily["burn_today"]
    burn_7d_avg = daily["burn_7d_avg"]
    weeks: list[DailyBar] = daily["weeks"]
    now_msk: datetime = snapshot["now_msk"]

    grid_html = _render_daily_grid(weeks, len(weeks) // 7)
    since_label = since.strftime("%d.%m")
    burn_html = _burn_label(burn_today, burn_7d_avg)
    now_label = now_msk.strftime("%Y-%m-%d %H:%M MSK")

    return f"""
<section class="daily-card">
  <div class="daily-card-head">
    <h1 class="daily-card-title">Daily · 4 weeks</h1>
    <div class="daily-card-meta">обновлено: {now_label}</div>
  </div>
  <div class="daily-card-body">
    {grid_html}
  </div>
  <div class="daily-card-foot">
    <span>4 недели · с Пн {since_label} · {burn_html}</span>
    <span class="daily-legend">
      Less
      <span class="daily-legend-cell intensity-L1" title="L1"></span>
      <span class="daily-legend-cell intensity-L2" title="L2"></span>
      <span class="daily-legend-cell intensity-L3" title="L3"></span>
      <span class="daily-legend-cell intensity-L4" title="L4"></span>
      More
    </span>
  </div>
</section>
"""


# ---- top-level render ----------------------------------------------------


_CSS = """
:root {
  --bg: #0f1115;
  --panel: #181b22;
  --panel-2: #1d2129;
  --ink: #f5f7fb;
  --ink-mute: #a0a8b8;
  --ink-faint: #6b7384;
  --border: #2a2f3a;
  --border-soft: #1f2530;
  --l1: #9be9a8;
  --l2: #40c463;
  --l3: #30a14e;
  --l4: #216e39;
  --empty: #2a2f3a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.4;
}
.daily-card {
  max-width: 720px;
  margin: 0 auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
}
.daily-card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 16px;
}
.daily-card-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}
.daily-card-meta {
  font-size: 12px;
  color: var(--ink-faint);
}
.daily-card-body {
  margin-bottom: 12px;
}
.daily-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--ink-mute);
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}
.daily-card-foot strong { color: var(--ink); font-weight: 600; }

.daily-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.daily-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.daily-col-head {
  font-size: 11px;
  color: var(--ink-faint);
  text-align: center;
  margin-bottom: 2px;
  letter-spacing: 0.04em;
}
.daily-col.current-week .daily-col-head {
  color: var(--ink);
  font-weight: 600;
}

.daily-cell {
  position: relative;
  border: 1px solid var(--border-soft);
  border-radius: 4px;
  padding: 6px 4px;
  text-align: center;
  background: var(--panel-2);
  min-height: 46px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 1px;
}
.daily-cell.current-week {
  border-color: var(--ink-faint);
  outline: 1px solid var(--ink-faint);
  outline-offset: -1px;
}
.daily-cell.state-future,
.daily-cell.state-empty {
  background: transparent;
  border-style: dashed;
  border-color: var(--border);
  color: var(--ink-faint);
}
.daily-cell.state-current {
  /* current — same shape as active, intensity-color если value>0 */
}
.daily-value {
  font-size: 13px;
  font-weight: 600;
  color: var(--ink);
  line-height: 1.1;
}
.daily-date {
  font-size: 10px;
  color: var(--ink-faint);
  line-height: 1.0;
}
.daily-cell.state-future .daily-value,
.daily-cell.state-empty .daily-value {
  color: var(--ink-faint);
  font-weight: 400;
}
.daily-cell.intensity-L1 { background: rgba(155, 233, 168, 0.18); }
.daily-cell.intensity-L2 { background: rgba(64, 196, 99, 0.32); }
.daily-cell.intensity-L3 { background: rgba(48, 161, 78, 0.55); }
.daily-cell.intensity-L4 { background: rgba(33, 110, 57, 0.85); }
.daily-cell.intensity-L4 .daily-value { color: #ffffff; }
.daily-cell.intensity-L3 .daily-value { color: #ffffff; }

.daily-legend {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.daily-legend-cell {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 2px;
  border: 1px solid var(--border-soft);
}
.daily-legend-cell.intensity-L1 { background: rgba(155, 233, 168, 0.18); }
.daily-legend-cell.intensity-L2 { background: rgba(64, 196, 99, 0.32); }
.daily-legend-cell.intensity-L3 { background: rgba(48, 161, 78, 0.55); }
.daily-legend-cell.intensity-L4 { background: rgba(33, 110, 57, 0.85); }
"""


def render(snapshot: dict) -> str:
    """Сгенерировать self-contained `analytics.html` (только heatmap-карточка).

    Минимальный каркас (как `dashboard.html` сейчас): `<!DOCTYPE html>`,
    `<head>` с inline CSS, `<meta http-equiv="refresh" content="60">`,
    `<body>` с одной карточкой. Никаких `<script>` — heatmap статичен.
    """
    card_html = _render_daily_card(snapshot)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="60" />
  <title>Daily Analytics — Token Usage</title>
  <style>{_CSS}</style>
</head>
<body>
{card_html}
</body>
</html>
"""
