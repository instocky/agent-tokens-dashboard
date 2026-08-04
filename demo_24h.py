"""demo_24h.py — render dashboard.html с подменённой датой/данными для визуальной проверки.

Использует ТОТ ЖЕ render_html из build_dashboard.py, но подсовывает синтетический
hourly-словарь «вчера, 14:00, с распределённой нагрузкой по часам». Это позволяет
увидеть, как карточка «Today · 24H Stream» выглядит с настоящим разнообразием
состояний (active, current, peak, empty, future), без необходимости ждать конца дня.

Подход: патчим build_dashboard.datetime.now(MSK), чтобы вернуть фиктивный
«сейчас» (2026-08-03 14:00 MSK). Затем собираем synthetic hourly, прогоняем
тот же pipeline main() (с импортом функций вместо повторного кода), пишем
tmp/dashboard_demo.html.

Запуск:  python demo_24h.py
Выход:   tmp/dashboard_demo.html
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_dashboard as bd  # noqa: E402
from build_dashboard import (  # noqa: E402
    MSK,
    WEEK_COUNT,
    WEEKLY_CAP_TOKENS,
    compute_current_hour,
    compute_current_window,
    compute_prev_day_today,
    compute_prev_hour,
    compute_prev_window_total,
    compute_sparkline_current,
    compute_sparkline_today,
    compute_sparkline_window,
    compute_today,
    compute_today_24h,
    compute_weekly,
    compute_weekly_threshold,
    current_window,
    fmt_delta_pct,
    render_html,
    today_24h_peak,
    _y_max_for,
    _y_ticks_for_log,
)

# ---- 1. Фиктивный «сейчас» --------------------------------------------------

# Вчера, 14:00 MSK — даёт разнообразие состояний:
#  - 00:00..13:59 (14 часов) — могут быть active/empty (часть прошедших)
#  - 14:00 — current
#  - 15:00..23:00 (9 часов) — future
DEMO_NOW = datetime(2026, 8, 3, 14, 0, 0, tzinfo=MSK)
DEMO_TODAY = DEMO_NOW.date()
DEMO_HOUR = DEMO_NOW.hour

# ---- 2. Синтетический hourly (распределение по часам) ----------------------

# Реалистичный паттерн: пик днём 12-14, минимум ночью 02-06, рабочий подъём 08-11.
# Значения — токены (input+output) за час.
HOURLY_PATTERN: dict[int, int] = {
    0:  120_000,   # поздний вечер
    1:   50_000,
    2:   30_000,   # глубокая ночь
    3:   20_000,
    4:   45_000,
    5:   90_000,
    6:  150_000,   # раннее утро
    7:  220_000,
    8:  410_000,   # начало работы
    9:  680_000,
    10: 850_000,
    11: 920_000,
    12: 1_100_000,  # approaching peak
    13: 1_350_000,  # peak
    14: 480_000,    # current (in-progress, 14:00 — только начался)
    # 15..23 = 0 (ещё нет данных → future, h > 14)
}

# Словарь hourly: сегодняшний день (DEMO_TODAY = 2026-08-03) + прошлые 7 дней
# (2026-07-27..2026-08-02), чтобы weekly chart тоже был непустой.
WEEKLY_PATTERN: dict[tuple[date, int], int] = {
    # Сегодня — сам HOURLY_PATTERN. 15..23 (9 часов) отсутствуют, но compute_*
    # вернёт для них 0 (т.е. h>14 → state="future").
    (DEMO_TODAY, h): v for h, v in HOURLY_PATTERN.items()
}
# Прошлая неделя (W-31, 27 июл - 2 авг) с похожим паттерном, разные множители.
WEEKLY_PATTERN |= {
    (date(2026, 7, 27), h): int(v * 0.6) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 7, 28), h): int(v * 0.9) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 7, 29), h): int(v * 1.1) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 7, 30), h): int(v * 0.5) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 7, 31), h): int(v * 0.85) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 8, 1),  h): int(v * 0.75) for h, v in HOURLY_PATTERN.items()
}
WEEKLY_PATTERN |= {
    (date(2026, 8, 2),  h): int(v * 1.0) for h, v in HOURLY_PATTERN.items()
}

# ---- 3. Прогон pipeline (тот же, что в main, но без CLI/args) -------------

# NB: monkey-patch datetime.now() НЕ нужен — все compute_* функции принимают
# now_msk параметром, и мы передаём DEMO_NOW явно. main() в build_dashboard
# сама вызывает datetime.now(MSK) (потому что это entry-point), а мы повторяем
# её логику, подменяя только now_msk.

def main() -> int:
    # Re-import после monkey-patch (модули уже импортированы, но пересоберём,
    # чтобы гарантированно использовать подменённые datetime/date).
    # На самом деле bd.* уже подменены, и весь код, который вызывает datetime.now()
    # через `bd.datetime.now()` или `datetime.now(MSK)` (top-level import),
    # получит DEMO_NOW — НО в build_dashboard.py datetime.now(MSK) импортирован
    # как `from datetime import date, datetime, timedelta, timezone` — а это
    # from-import, monkey-patch НЕ подменяет.
    # Решение: явно пересчитаем всё, что зависит от now_msk, руками.

    today = DEMO_TODAY
    now_msk = DEMO_NOW

    # Заглушка для 4-недельного since: с понедельника 3 недели назад от demo today.
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    since = current_monday - timedelta(weeks=WEEK_COUNT - 1)

    # Синтетические hourly — не открываем SQLite. Передаём напрямую.
    hourly = WEEKLY_PATTERN

    current_hour_tokens = compute_current_hour(hourly, today)
    today_tokens = compute_today(hourly, now_msk)
    window_total, window_entries, window_label = compute_current_window(hourly, now_msk)
    window_wraps = current_window(now_msk)["wraps"]
    weeks = compute_weekly(hourly, today)

    spark_current = compute_sparkline_current(hourly, now_msk)
    spark_today = compute_sparkline_today(hourly, now_msk)
    spark_window = compute_sparkline_window(window_entries)
    prev_hour = compute_prev_hour(hourly, now_msk)
    prev_day = compute_prev_day_today(hourly, now_msk)
    prev_window = compute_prev_window_total(hourly, now_msk)

    y_max = _y_max_for(weeks)
    log_info = _y_ticks_for_log(weeks)

    current_week = weeks[-1]
    today_idx = now_msk.weekday()
    today_spent = current_week.days[today_idx] or 0
    days_left = 8 - now_msk.isoweekday()
    weekly_threshold = compute_weekly_threshold(
        WEEKLY_CAP_TOKENS, today_spent, days_left
    )

    today_24h_bars = compute_today_24h(hourly, now_msk)
    today_24h_peak_val = today_24h_peak(today_24h_bars)

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
        today_24h=today_24h_bars,
        today_24h_peak=today_24h_peak_val,
        now_msk=now_msk,
    )

    out_path = Path(__file__).resolve().parent / "tmp" / "dashboard_demo.html"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[demo] wrote {out_path} ({len(html):,} bytes)")
    print(f"[demo] DEMO_NOW = {DEMO_NOW}")
    print(f"[demo] today_24h peak = {today_24h_peak_val}")
    print(f"[demo] today_24h bars: "
          f"active={sum(1 for b in today_24h_bars if b.state == 'active')}, "
          f"current={sum(1 for b in today_24h_bars if b.state == 'current')}, "
          f"peak={sum(1 for b in today_24h_bars if b.state == 'peak')}, "
          f"empty={sum(1 for b in today_24h_bars if b.state == 'empty')}, "
          f"future={sum(1 for b in today_24h_bars if b.state == 'future')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
