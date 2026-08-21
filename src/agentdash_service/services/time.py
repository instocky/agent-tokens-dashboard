"""Time and window helpers. No I/O, no DB."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import TZ


def now_in_tz() -> datetime:
    """Current time in configured TZ."""
    return datetime.now(TZ)


def since_midnight_ms(d: date, tz: ZoneInfo) -> int:
    """Unix epoch ms for midnight of `d` in `tz`."""
    midnight = datetime.combine(d, datetime.min.time(), tzinfo=tz)
    return int(midnight.timestamp() * 1000)


def current_week_window(today: date, week_count: int) -> tuple[date, date]:
    """Return (start_monday, current_monday) for the rolling window.

    Window spans `week_count` finished ISO weeks back through the start
    of the current week. Today's day-of-week is NOT subtracted — today
    sits within the current week and is included.
    """
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    start_monday = current_monday - timedelta(weeks=week_count)
    return start_monday, current_monday


def start_of_window_ms(today: date, tz: ZoneInfo, week_count: int) -> int:
    """Unix epoch ms for the start of the rolling window."""
    start_monday, _ = current_week_window(today, week_count)
    return since_midnight_ms(start_monday, tz)


def end_of_now_ms(now: datetime) -> int:
    """Unix epoch ms for `now` — used to truncate the SQL filter."""
    return int(now.timestamp() * 1000)


def days_left_in_week(today: date) -> int:
    """Days remaining in the current ISO week, including today.

    Monday = 1, Sunday = 7. So if today is Wednesday, days_left = 5
    (Wed/Thu/Fri/Sat/Sun).
    """
    return 8 - today.isocalendar()[2]


# 5-hour windows for the "today" breakdown on the dashboard.
# `night` wraps midnight (23, 0, 1, 2). Others are contiguous.
WINDOWS: list[dict] = [
    {"name": "morning",   "hours": [3, 4, 5, 6, 7],     "label": "03:00–08:00", "wraps": False},
    {"name": "midday",    "hours": [8, 9, 10, 11, 12],  "label": "08:00–13:00", "wraps": False},
    {"name": "afternoon", "hours": [13, 14, 15, 16, 17], "label": "13:00–18:00", "wraps": False},
    {"name": "evening",   "hours": [18, 19, 20, 21, 22], "label": "18:00–23:00", "wraps": False},
    {"name": "night",     "hours": [23, 0, 1, 2],       "label": "23:00–03:00", "wraps": True},
]
