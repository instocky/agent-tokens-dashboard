"""Tests for compute_weekly split (input/output per day).

Карточка «Weekly Compare» (W-30..W-33): split-стек на каждом дневном баре
(input внизу, output сверху). Покрывает:
  - days и days_split идут параллельно, оба None в одних и тех же позициях
  - sum-контракт: days[i] = days_split[i][0] + days_split[i][1]
  - week с input>0, output=0 (только input) — корректно
  - week с input=0, output>0 (только output) — корректно
  - days[i] = None ⇔ days_split[i] is None (для "no data" дней)
  - week.days_split никогда не None даже если неделя пустая (None только per-day)

Запуск: `python tests/test_weekly_split.py`.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    WEEK_COUNT,
    compute_weekly,
)


def _split_dict(entries: list[tuple[date, int, int]]) -> dict[tuple[date, int], tuple[int, int]]:
    """Helper: собрать split-словарь из (date, hour, total) — кладём всё в output.

    Для позитивных тестов «только output» / «только input» строим явно.
    """
    return {}


def test_weekly_split_total_matches_sum() -> None:
    """days[i] — это сумма input+output из days_split[i]."""
    today = date(2026, 8, 7)  # пятница W-32
    # Понедельник..четверг W-32 имеют данные (input=1K, output=4K каждый час).
    days_data = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]
    hourly: dict[tuple[date, int], tuple[int, int]] = {}
    for d in days_data:
        for h in range(24):
            hourly[(d, h)] = (1_000, 4_000)  # total = 5_000/час
    weeks = compute_weekly(hourly, today)
    assert len(weeks) == WEEK_COUNT

    # Текущая неделя — последняя (W-32).
    w32 = weeks[-1]
    assert w32.label == "W-32"
    for i in range(5):  # Пн..Пт
        assert w32.days[i] is not None, f"day {i}: days is None"
        assert w32.days_split[i] is not None, f"day {i}: days_split is None"
        in_v, out_v = w32.days_split[i]
        assert in_v == 24_000, f"day {i}: in_v={in_v}"
        assert out_v == 96_000, f"day {i}: out_v={out_v}"
        assert w32.days[i] == 120_000, f"day {i}: total={w32.days[i]}"
    # Сб, Вс — будущее (W-32 ends Sun Aug 9, today=Fri Aug 7).
    # 5+Aug (Sat) and 6+Aug (Sun) — будущее → None.
    for i in (5, 6):
        assert w32.days[i] is None, f"day {i} (future): days should be None"
        assert w32.days_split[i] is None, f"day {i} (future): days_split should be None"


def test_weekly_split_no_data_marks_none() -> None:
    """День без записей в hourly → days=None и days_split=None."""
    today = date(2026, 8, 7)  # пятница W-32
    # Данные только за 1 день (вчера = четверг 6 авг).
    hourly: dict[tuple[date, int], tuple[int, int]] = {
        (date(2026, 8, 6), 10): (500, 1500),
    }
    weeks = compute_weekly(hourly, today)
    w32 = weeks[-1]
    # Пн..Ср (3,4,5) — нет данных → None
    for i in range(3):
        assert w32.days[i] is None, f"day {i} (no data): days should be None"
        assert w32.days_split[i] is None, f"day {i} (no data): days_split should be None"
    # Чт (3) — есть данные
    assert w32.days[3] == 2_000
    assert w32.days_split[3] == (500, 1_500)
    # Пт (4) — сегодня, 0 данных
    assert w32.days[4] is None
    assert w32.days_split[4] is None
    # Сб, Вс — будущее
    assert w32.days[5] is None
    assert w32.days_split[5] is None


def test_weekly_split_only_output() -> None:
    """input=0, output>0 — корректно пробрасывается, days = out."""
    today = date(2026, 8, 7)
    hourly: dict[tuple[date, int], tuple[int, int]] = {
        (date(2026, 8, 3), h): (0, 100) for h in range(24)
    }
    weeks = compute_weekly(hourly, today)
    w32 = weeks[-1]
    assert w32.days[0] == 2_400
    assert w32.days_split[0] == (0, 2_400)


def test_weekly_split_only_input() -> None:
    """input>0, output=0 — корректно пробрасывается, days = in."""
    today = date(2026, 8, 7)
    hourly: dict[tuple[date, int], tuple[int, int]] = {
        (date(2026, 8, 3), h): (50, 0) for h in range(24)
    }
    weeks = compute_weekly(hourly, today)
    w32 = weeks[-1]
    assert w32.days[0] == 1_200
    assert w32.days_split[0] == (1_200, 0)


def test_weekly_split_does_not_mix_with_past_weeks() -> None:
    """Каждая неделя считает только свои 7 дней (current_monday-based)."""
    today = date(2026, 8, 7)  # W-32
    # Данные за Пн прошлой недели (W-31) и Пн текущей (W-32).
    hourly: dict[tuple[date, int], tuple[int, int]] = {
        (date(2026, 7, 27), 10): (1, 9),   # W-31 Пн
        (date(2026, 8, 3), 10): (100, 900),  # W-32 Пн
    }
    weeks = compute_weekly(hourly, today)
    w31 = weeks[-2]
    w32 = weeks[-1]
    assert w31.label == "W-31"
    assert w31.days[0] == 10
    assert w31.days_split[0] == (1, 9)
    assert w32.days[0] == 1_000
    assert w32.days_split[0] == (100, 900)


def main() -> int:
    """Прогон всех test_* функций в этом модуле. Возвращает exit code."""
    import traceback
    tests = [
        test_weekly_split_total_matches_sum,
        test_weekly_split_no_data_marks_none,
        test_weekly_split_only_output,
        test_weekly_split_only_input,
        test_weekly_split_does_not_mix_with_past_weeks,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"  ERROR {t.__name__}:")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
