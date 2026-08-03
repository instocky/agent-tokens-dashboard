"""Boundary tests for current_window() slot selection.

Запускается напрямую: `python tests/test_windows.py`.
Не pytest — прототип без test infra, и этого достаточно для регрессии
на граничные часы. При добавлении pytest/pyproject — переезд тривиальный.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Чтобы import работал и при запуске из корня, и из tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    MSK,
    current_window,
    compute_current_window,
)


# (hour, expected_label, expected_wraps, expected_hours)
# Граничные часы — каждая граница между слотами должна попасть в правильный.
BOUNDARIES: list[tuple[int, str, bool, list[int]]] = [
    # ---- slot 0: 03:00-07:59 ----
    (3,  "03:00–07:59", False, [3, 4, 5, 6, 7]),
    (4,  "03:00–07:59", False, [3, 4, 5, 6, 7]),
    (7,  "03:00–07:59", False, [3, 4, 5, 6, 7]),
    # ---- slot 1: 08:00-12:59 ----
    (8,  "08:00–12:59", False, [8, 9, 10, 11, 12]),
    (12, "08:00–12:59", False, [8, 9, 10, 11, 12]),
    # ---- slot 2: 13:00-17:59 ----
    (13, "13:00–17:59", False, [13, 14, 15, 16, 17]),
    (17, "13:00–17:59", False, [13, 14, 15, 16, 17]),
    # ---- slot 3: 18:00-22:59 ----
    (18, "18:00–22:59", False, [18, 19, 20, 21, 22]),
    (22, "18:00–22:59", False, [18, 19, 20, 21, 22]),
    # ---- slot 4: 23:00-02:59 (wraps) ----
    (23, "23:00–02:59", True,  [23, 0, 1, 2]),
    (0,  "23:00–02:59", True,  [23, 0, 1, 2]),
    (1,  "23:00–02:59", True,  [23, 0, 1, 2]),
    (2,  "23:00–02:59", True,  [23, 0, 1, 2]),
]


def _now(hour: int) -> datetime:
    """datetime в MSK с заданным часом (минуты/секунды = 0)."""
    return datetime(2026, 8, 3, hour, 0, 0, tzinfo=MSK)


def test_current_window_boundaries() -> None:
    failures: list[str] = []
    for hour, expected_label, expected_wraps, expected_hours in BOUNDARIES:
        w = current_window(_now(hour))
        if w["label"] != expected_label:
            failures.append(
                f"hour={hour:02d}: label {w['label']!r} != {expected_label!r}"
            )
        if w["wraps"] != expected_wraps:
            failures.append(
                f"hour={hour:02d}: wraps {w['wraps']} != {expected_wraps}"
            )
        if w["hours"] != expected_hours:
            failures.append(
                f"hour={hour:02d}: hours {w['hours']} != {expected_hours}"
            )
    if failures:
        raise AssertionError(
            "current_window() boundary failures:\n  " + "\n  ".join(failures)
        )


def test_compute_current_window_day_slot() -> None:
    """Дневной слот: 5 entries, все с одной датой, total = сумма hourly[(day, h)]."""
    today = datetime(2026, 8, 3, tzinfo=MSK).date()
    hourly: dict[tuple, int] = {
        (today, 13): 100,
        (today, 14): 200,
        (today, 15): 300,
        (today, 16): 0,
        (today, 17): 400,
        # шум вне слота
        (today, 12): 999,
        (today, 18): 999,
    }
    now = _now(14)
    total, entries, label = compute_current_window(hourly, now)
    assert label == "13:00–17:59", f"label={label}"
    assert total == 1000, f"total={total}"  # 100+200+300+0+400
    assert [h for h, _, _ in entries] == [13, 14, 15, 16, 17]
    assert all(d == today for _, _, d in entries), "all entries must be today"


def test_compute_current_window_night_slot() -> None:
    """Ночной слот в 01:00: 23 из вчера + 0/1 из сегодня, total корректный."""
    today = datetime(2026, 8, 3, tzinfo=MSK).date()
    yesterday = today - timedelta(days=1)
    hourly: dict[tuple, int] = {
        (yesterday, 23): 50,
        (today, 0): 60,
        (today, 1): 70,
        (today, 2): 80,
        # шум
        (today, 3): 999,
        (yesterday, 22): 999,
    }
    now = _now(1)
    total, entries, label = compute_current_window(hourly, now)
    assert label == "23:00–02:59", f"label={label}"
    assert total == 50 + 60 + 70 + 80, f"total={total}"
    assert [h for h, _, _ in entries] == [23, 0, 1, 2]
    # 23 → вчера, 0/1/2 → сегодня
    assert entries[0][2] == yesterday
    assert entries[1][2] == today
    assert entries[2][2] == today
    assert entries[3][2] == today


def test_compute_current_window_missing_data() -> None:
    """Если в hourly ничего нет — total=0, entries=5 с нулями."""
    today = datetime(2026, 8, 3, tzinfo=MSK).date()
    now = _now(13)
    total, entries, label = compute_current_window({}, now)
    assert label == "13:00–17:59"
    assert total == 0
    assert len(entries) == 5
    assert all(v == 0 for _, v, _ in entries)


def main() -> int:
    tests = [
        test_current_window_boundaries,
        test_compute_current_window_day_slot,
        test_compute_current_window_night_slot,
        test_compute_current_window_missing_data,
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
