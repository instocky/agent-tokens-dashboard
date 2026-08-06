"""demo_24h.py — render dashboard.html с подменённым снимком данных для визуальной проверки.

Использует ТОТ ЖЕ render(snapshot) из render_dashboard.py, но подсовывает
синтетический snapshot «вчера, 14:00, с распределённой нагрузкой по часам».
Это позволяет увидеть, как карточка «Today · 24H Stream» выглядит с
настоящим разнообразием состояний (active, current, peak, empty, future),
без необходимости ждать конца дня.

Подход: собираем синтетический hourly-словарь, прогоняем через те же
compute_* функции, что и analytics.build_snapshot, склеиваем snapshot
dict по контракту ADR §2.4 и передаём в render_dashboard.render(snapshot).
Никаких monkey-patch и SQLite — synthetic pipeline целиком в памяти.

Запуск:  python demo_24h.py
Выход:   tmp/dashboard_demo.html
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analytics import (  # noqa: E402
    MSK,
    WEEK_COUNT,
    WEEKLY_CAP_TOKENS,
    compute_current_hour,
    compute_current_window,
    compute_prev_day_today,
    compute_prev_hour,
    compute_prev_window_total,
    compute_today,
    compute_today_24h,
    compute_weekly,
    compute_weekly_threshold,
    current_window,
    fmt_delta_pct,
    pill_level,
    today_24h_peak,
)
import render_dashboard  # noqa: E402

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

# ---- 3. Сборка snapshot по контракту §2.4 ADR ------------------------------

def _build_demo_snapshot() -> dict:
    """Собрать snapshot-дискт из синтетического hourly, по тому же контракту
    что analytics.build_snapshot (ADR §2.4).

    Демо-данные, для которых нет синтетического эквивалента (DB-side:
    today_meta, current_session) — подобраны как realistic-кейс. Это явно
    «демо», а не реальные данные; см. комментарии в build_snapshot в
    analytics.py для прод-пути.
    """
    today = DEMO_TODAY
    now_msk = DEMO_NOW

    # Заглушка для 4-недельного since: с понедельника 3 недели назад от demo today.
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    since = current_monday - timedelta(weeks=WEEK_COUNT - 1)

    hourly = WEEKLY_PATTERN

    current_hour_tokens = compute_current_hour(hourly, today)
    today_tokens = compute_today(hourly, now_msk)
    window_total, window_entries, window_label = compute_current_window(hourly, now_msk)
    window_wraps = current_window(now_msk)["wraps"]
    weeks = compute_weekly(hourly, today)

    prev_hour = compute_prev_hour(hourly, now_msk)
    prev_day = compute_prev_day_today(hourly, now_msk)
    prev_window = compute_prev_window_total(hourly, now_msk)

    y_max = render_dashboard._y_max_for(weeks)
    log_info = render_dashboard._y_ticks_for_log(weeks)

    current_week = weeks[-1]
    today_idx = now_msk.weekday()
    today_spent = current_week.days[today_idx] or 0
    days_left = 8 - now_msk.isoweekday()
    weekly_threshold = compute_weekly_threshold(
        WEEKLY_CAP_TOKENS, today_spent, days_left
    )

    today_24h_bars = compute_today_24h(hourly, now_msk)
    today_24h_peak_val = today_24h_peak(today_24h_bars)

    # Демо-данные для today_meta (sub-line карточки «Сегодня»). Подобраны
    # под реалистичный кейс 2026-08-04: 2 сессии, 7 user-сообщений, avg=3.5.
    # В demo БД не открываем — synthetic pipeline.
    today_sessions = 2
    today_user_requests = 7
    today_avg = 3.5
    # avg_tokens_per_session — производное от today_tokens/sessions, как в main().
    avg_tokens_per_session: int | None = (
        int(today_tokens / today_sessions) if today_sessions > 0 else None
    )

    # Демо-данные для current_session (используются в hero-pill'е сессии).
    # В demo БД не открываем, synthetic значения подобраны под реалистичный
    # случай: текущая сессия в работе, 50K токенов, 5 запросов.
    # NB: title/record_title/duration_ms ставим None — это сохраняет
    # byte-identity с пре-рефакторным demo, который не передавал эти kwargs
    # (default None в старом render_html). path оставляем — раньше demo
    # задавал его вручную для tooltip. Если захочется показывать проект/путь
    # в demo — отдельный коммит и обновить эталон tmp/dashboard_demo.html.
    current_session_tokens = 50_000
    current_session_requests = 5
    current_session_path = "C:/Projects/demo-project"
    current_session_title = None
    current_session_record_title = None
    current_session_duration_ms = None

    snapshot = {
        "now_msk": now_msk,
        "hour": {"tokens": current_hour_tokens},
        "today": {
            "tokens": today_tokens,
            "sessions": today_sessions,
            "user_requests": today_user_requests,
            "avg": today_avg,
            "avg_tokens_per_session": avg_tokens_per_session,
            "bars_24h": today_24h_bars,
            "peak_24h": today_24h_peak_val,
        },
        "window": {
            "total": window_total,
            "entries": window_entries,
            "label": window_label,
            "wraps": window_wraps,
        },
        "weekly": {
            "since": since,
            "weeks": weeks,
            "cap": WEEKLY_CAP_TOKENS,
            "today_spent": today_spent,
            "days_left": days_left,
            "threshold": weekly_threshold,
            "day_level": pill_level(today_spent, weekly_threshold),
        },
        "session": {
            "id": "demo-session",
            "tokens": current_session_tokens,
            "requests": current_session_requests,
            "path": current_session_path,
            "project_title": current_session_title,
            "record_title": current_session_record_title,
            "duration_ms": current_session_duration_ms,
            "level": pill_level(current_session_tokens, avg_tokens_per_session),
        },
    }
    # Suppress unused — prev_* derived values are part of the old render_html
    # signature; render(snapshot) doesn't need them at module top-level, but
    # we keep the calls so demo's data flow mirrors analytics.build_snapshot.
    _ = (prev_hour, prev_day, prev_window, y_max, log_info, fmt_delta_pct)
    return snapshot


def main() -> int:
    snapshot = _build_demo_snapshot()
    html = render_dashboard.render(snapshot)

    out_path = Path(__file__).resolve().parent / "tmp" / "dashboard_demo.html"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    today = snapshot["today"]
    print(f"[demo] wrote {out_path} ({len(html):,} bytes)")
    print(f"[demo] DEMO_NOW = {snapshot['now_msk']}")
    if today["peak_24h"] is not None:
        ph, pv = today["peak_24h"]
        print(f"[demo] today_24h peak = ({ph}, {pv})")
    bars = today["bars_24h"]
    print(
        f"[demo] today_24h bars: "
        f"active={sum(1 for b in bars if b.state == 'active')}, "
        f"current={sum(1 for b in bars if b.state == 'current')}, "
        f"peak={sum(1 for b in bars if b.state == 'peak')}, "
        f"empty={sum(1 for b in bars if b.state == 'empty')}, "
        f"future={sum(1 for b in bars if b.state == 'future')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
