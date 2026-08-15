"""Tests for collect_time_series split (input/output) в project-dashboard.

Покрывает:
  - SQL возвращает (input, output) раздельно, а не только total
  - per-project dicts days/days_split/hourly split параллельны: total == in+out
  - пустые дни между активностями дополняются (0, 0) в days_split
  - non-overlap с другими проектами (sid_to_project фильтрует)
  - empty join (sid_to_project={}) → {}

Запуск: `python tests/test_project_split.py`.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_project_dashboard import (  # noqa: E402
    MSK,
    collect_time_series,
)


def _make_db() -> sqlite3.Connection:
    """In-memory SQLite с минимальной schema под collect_time_series.

    Только local_runtime_token_usage нужна для collect_time_series;
    sid_to_project передаётся извне.
    """
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE local_runtime_token_usage (
            session_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        );
    """)
    return con


def _insert_usage(
    con: sqlite3.Connection,
    sid: str,
    entries: list[tuple[int, int, int]],  # (ts_ms, in, out)
) -> None:
    for ts, in_t, out_t in entries:
        con.execute(
            "INSERT INTO local_runtime_token_usage VALUES (?, ?, ?, ?)",
            (sid, ts, in_t, out_t),
        )


def _msk_midnight(d: date) -> int:
    """MSK midnight → unix ms (для границ SQL-фильтра)."""
    dt = datetime.combine(d, datetime.min.time(), tzinfo=MSK)
    return int(dt.timestamp() * 1000)


def _msk_at(d: date, hour: int, minute: int = 0) -> int:
    """(MSK date, hour, minute) → unix ms (для подстановки turn'ов)."""
    dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=MSK)
    return int(dt.timestamp() * 1000)


# ---- SQL shape --------------------------------------------------------------

def test_collect_time_series_returns_split_per_hour() -> None:
    """Один session с одним turn'ом → ProjectTimeSeries с split (in, out)."""
    con = _make_db()
    ts = _msk_at(date(2026, 8, 7), 12)
    _insert_usage(con, "s1", [(ts, 100, 200)])

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(con, start_ms, end_ms, {"s1": "alpha"})
    assert "alpha" in ts_map
    series = ts_map["alpha"]
    # 2026-08-07 12:00 MSK → date 2026-08-07, hour 12.
    assert series.hours[(date(2026, 8, 7), 12)] == 300
    assert series.hours_split[(date(2026, 8, 7), 12)] == (100, 200)
    assert series.days[date(2026, 8, 7)] == 300
    assert series.days_split[date(2026, 8, 7)] == (100, 200)
    # 100 + 200 == 300 (parity).
    assert series.hours_split[(date(2026, 8, 7), 12)][0] + \
           series.hours_split[(date(2026, 8, 7), 12)][1] == 300


def test_collect_time_series_groups_by_session_id() -> None:
    """Несколько turn'ов одной сессии за разные часы → split-агрегаты по часам.

    NB: один и тот же sid, разные ts — split суммируется в Python
    (тот же паттерн, что в build_dashboard.py::aggregate_by_hour_split).
    """
    con = _make_db()
    h10 = _msk_at(date(2026, 8, 7), 10)
    h11 = _msk_at(date(2026, 8, 7), 11)
    _insert_usage(
        con, "s1",
        [(h10, 50, 25), (h11, 70, 35)],
    )

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(con, start_ms, end_ms, {"s1": "alpha"})
    series = ts_map["alpha"]

    # Per-hour split.
    assert series.hours[(date(2026, 8, 7), 10)] == 75
    assert series.hours_split[(date(2026, 8, 7), 10)] == (50, 25)
    assert series.hours[(date(2026, 8, 7), 11)] == 105
    assert series.hours_split[(date(2026, 8, 7), 11)] == (70, 35)
    # Per-day split — суммируется.
    assert series.days[date(2026, 8, 7)] == 180
    assert series.days_split[date(2026, 8, 7)] == (120, 60)


def test_collect_time_series_filters_by_sid_to_project() -> None:
    """Turn'ы от session_id, которых нет в sid_to_project, → skip.

    Защита от cross-project contamination (как в build_dashboard.py).
    """
    con = _make_db()
    ts = _msk_at(date(2026, 8, 7), 12)
    _insert_usage(con, "s_unknown", [(ts, 100, 200)])

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    # s_unknown не в sid_to_project → результат пустой.
    ts_map = collect_time_series(
        con, start_ms, end_ms, {"s_other": "alpha"}
    )
    assert ts_map == {}, f"expected empty, got {ts_map}"


def test_collect_time_series_empty_sid_to_project() -> None:
    """Пустой sid_to_project → {} (без SQL-вызова)."""
    con = _make_db()
    ts_map = collect_time_series(con, 0, 1, {})
    assert ts_map == {}


def test_collect_time_series_skips_zero_total_buckets() -> None:
    """Turn с in=out=0 не даёт bucket (как раньше total<=0 → skip)."""
    con = _make_db()
    ts = _msk_at(date(2026, 8, 7), 12)
    _insert_usage(con, "s1", [(ts, 0, 0)])

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(con, start_ms, end_ms, {"s1": "alpha"})
    assert ts_map == {}, f"expected empty, got {ts_map}"


# ---- days_split non-continuous fill ---------------------------------------

def test_collect_time_series_fills_gap_days_with_zero_split() -> None:
    """Пустые дни между активностями: days имеет 0, days_split имеет (0, 0)."""
    con = _make_db()
    # 2026-08-07 и 2026-08-10 — пропуск 08-08 и 08-09.
    h_07 = _msk_at(date(2026, 8, 7), 12)
    h_10 = _msk_at(date(2026, 8, 10), 12)
    _insert_usage(con, "s1", [(h_07, 100, 50), (h_10, 200, 80)])

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(con, start_ms, end_ms, {"s1": "alpha"})
    series = ts_map["alpha"]

    # 7..10 — непрерывный ряд (4 дня), включая пустые.
    expected_dates = [date(2026, 8, d) for d in range(7, 11)]
    assert list(series.days.keys()) == expected_dates, (
        f"got days={list(series.days.keys())}"
    )
    # 8 и 9 — пустые, должны быть в days как 0, в days_split как (0, 0).
    assert series.days[date(2026, 8, 8)] == 0
    assert series.days_split[date(2026, 8, 8)] == (0, 0)
    assert series.days[date(2026, 8, 9)] == 0
    assert series.days_split[date(2026, 8, 9)] == (0, 0)
    # 7 и 10 — данные.
    assert series.days[date(2026, 8, 7)] == 150
    assert series.days_split[date(2026, 8, 7)] == (100, 50)
    assert series.days[date(2026, 8, 10)] == 280
    assert series.days_split[date(2026, 8, 10)] == (200, 80)


# ---- isolation per project -------------------------------------------------

def test_collect_time_series_isolates_projects() -> None:
    """Два проекта, разные sid — split НЕ утекает между ними."""
    con = _make_db()
    ts_a = _msk_at(date(2026, 8, 7), 12)
    ts_b = _msk_at(date(2026, 8, 7), 13)
    _insert_usage(con, "s_a", [(ts_a, 100, 50)])
    _insert_usage(con, "s_b", [(ts_b, 200, 80)])

    start_ms = _msk_midnight(date(2026, 8, 1))
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(
        con, start_ms, end_ms, {"s_a": "alpha", "s_b": "beta"}
    )
    assert ts_map["alpha"].days_split[date(2026, 8, 7)] == (100, 50)
    assert ts_map["beta"].days_split[date(2026, 8, 7)] == (200, 80)
    # Cross-check: суммы split'ов равны total'ам.
    a_in, a_out = ts_map["alpha"].days_split[date(2026, 8, 7)]
    assert a_in + a_out == ts_map["alpha"].days[date(2026, 8, 7)]
    b_in, b_out = ts_map["beta"].days_split[date(2026, 8, 7)]
    assert b_in + b_out == ts_map["beta"].days[date(2026, 8, 7)]


# ---- first/last day bounds -------------------------------------------------

def test_collect_time_series_first_last_day_bounded_by_activity() -> None:
    """first_day/last_day — границы именно активности, не окна SQL."""
    con = _make_db()
    # Активность только 2026-08-05 и 2026-08-08, окно шире.
    h_05 = _msk_at(date(2026, 8, 5), 12)
    h_08 = _msk_at(date(2026, 8, 8), 12)
    _insert_usage(con, "s1", [(h_05, 10, 5), (h_08, 20, 10)])

    start_ms = _msk_midnight(date(2026, 8, 1))  # окно шире активности
    end_ms = _msk_midnight(date(2026, 8, 31))
    ts_map = collect_time_series(con, start_ms, end_ms, {"s1": "alpha"})
    series = ts_map["alpha"]
    assert series.first_day == date(2026, 8, 5)
    assert series.last_day == date(2026, 8, 8)
    # Полный ряд — 5..8 (4 дня, непрерывный).
    assert list(series.days.keys()) == [
        date(2026, 8, 5), date(2026, 8, 6),
        date(2026, 8, 7), date(2026, 8, 8),
    ]


# ---- main ------------------------------------------------------------------

def main() -> int:
    tests = [
        test_collect_time_series_returns_split_per_hour,
        test_collect_time_series_groups_by_session_id,
        test_collect_time_series_filters_by_sid_to_project,
        test_collect_time_series_empty_sid_to_project,
        test_collect_time_series_skips_zero_total_buckets,
        test_collect_time_series_fills_gap_days_with_zero_split,
        test_collect_time_series_isolates_projects,
        test_collect_time_series_first_last_day_bounded_by_activity,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}\n    {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
