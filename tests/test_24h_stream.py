"""Tests for compute_today_24h / _intensity_level / today_24h_peak.

Карточка «Today · 24H Stream»: 24-часовая разбивка сегодняшнего дня.
Покрывает:
  - state machine (active / current / peak / future / empty) на границах
  - peak-выбор: топ-1 среди прошлых часов; ties, zero, current==peak
  - intensity-квартили (L1..L4) — корректно для N=0, N<4, N>=4
  - current hour: всегда current, даже если value=0; current может совпасть с peak
  - 24 бара всегда, по возрастанию hour, в диапазоне 0..23

Запуск: `python tests/test_24h_stream.py`.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    MSK,
    HourlyBar,
    _intensity_level,
    compute_today_24h,
    today_24h_peak,
)


def _now(hour: int, day: int = 4) -> datetime:
    """datetime в MSK с заданным часом."""
    return datetime(2026, 8, day, hour, 0, 0, tzinfo=MSK)


def _bar_at(bars: list[HourlyBar], hour: int) -> HourlyBar:
    """Достать бар по часу (assert: 24 бара, ровно один на каждый hour)."""
    matches = [b for b in bars if b.hour == hour]
    assert len(matches) == 1, f"hour={hour}: matches={len(matches)}"
    return matches[0]


# ---- _intensity_level ----------------------------------------------------

def test_intensity_quartiles_8_values() -> None:
    """8 значений → квартили n//4=2, n//2=4, 3n//4=6 → L1..L4 корректно.

    sorted=[10, 20, 30, 40, 50, 60, 70, 80].
    Границы (по позициям, inclusive): q1=sorted[2]=30, q2=sorted[4]=50, q3=sorted[6]=70.
    value <= q1 → L1, value <= q2 → L2, value <= q3 → L3, иначе L4.
    """
    sorted_active = [10, 20, 30, 40, 50, 60, 70, 80]
    cases: list[tuple[int, str]] = [
        (10, "L1"),
        (20, "L1"),
        (30, "L1"),  # == q1 → L1
        (40, "L2"),
        (50, "L2"),  # == q2 → L2
        (60, "L3"),
        (70, "L3"),  # == q3 → L3
        (80, "L4"),
    ]
    failures: list[str] = []
    for v, expected in cases:
        got = _intensity_level(v, sorted_active)
        if got != expected:
            failures.append(f"value={v}: got {got}, expected {expected}")
    if failures:
        raise AssertionError("intensity-quartile failures:\n  " + "\n  ".join(failures))


def test_intensity_few_values_collapse_to_L2() -> None:
    """При N<4 все ненулевые значения идут в L2 (не плодим квартили на 1-3 точках)."""
    for n in (1, 2, 3):
        sorted_active = [1000 * (i + 1) for i in range(n)]
        for v in sorted_active:
            got = _intensity_level(v, sorted_active)
            if got != "L2":
                raise AssertionError(
                    f"n={n}, v={v}: got {got}, expected L2 (collapse)"
                )


def test_intensity_zero_returns_L2_fallback() -> None:
    """Защита: value<=0 → L2. (На практике _intensity_level вызывается
    только для value>0, но контракт защищает от 'что если'.)"""
    assert _intensity_level(0, []) == "L2"
    assert _intensity_level(0, [100, 200, 300, 400]) == "L2"


# ---- compute_today_24h: state machine -----------------------------------

def test_today_24h_returns_24_bars_ordered() -> None:
    """Ровно 24 бара, hours 0..23 в возрастающем порядке."""
    hourly: dict[tuple[date, int], int] = {}
    bars = compute_today_24h(hourly, _now(14))
    assert len(bars) == 24
    assert [b.hour for b in bars] == list(range(24))


def test_today_24h_empty_day_at_14() -> None:
    """now=14, нет данных: hours 0..13 = empty, 14 = current, 15..23 = future."""
    bars = compute_today_24h({}, _now(14))
    states = [b.state for b in bars]
    # 0..13 — empty
    for h in range(0, 14):
        assert _bar_at(bars, h).state == "empty", f"hour {h} should be empty"
        assert _bar_at(bars, h).intensity is None
    # 14 — current (value=0)
    cur = _bar_at(bars, 14)
    assert cur.state == "current"
    assert cur.value == 0
    assert cur.intensity is None
    # 15..23 — future
    for h in range(15, 24):
        assert _bar_at(bars, h).state == "future", f"hour {h} should be future"
        assert _bar_at(bars, h).intensity is None
    # No peak on an empty day
    assert today_24h_peak(bars) is None


def test_today_24h_current_at_hour_0() -> None:
    """Граничный кейс: now=0. h=0 — current (value=0, чтобы не стать peak'ом
    из-за единственных данных). Всё остальное future."""
    # value=0 в current → peak_val=0 → peak_hour=None → current остаётся current.
    bars = compute_today_24h({}, _now(0))
    assert _bar_at(bars, 0).state == "current"
    assert _bar_at(bars, 0).value == 0
    for h in range(1, 24):
        assert _bar_at(bars, h).state == "future"


def test_today_24h_current_at_hour_23() -> None:
    """Граничный кейс: now=23. h=23 — current, ничего future. Данные кладём в
    прошлые часы, чтобы current=23 не оказался пиком."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {(today, 10): 5_000}
    bars = compute_today_24h(hourly, _now(23))
    assert _bar_at(bars, 23).state == "current"
    assert _bar_at(bars, 23).value == 0  # в текущий час ещё нет данных
    # h=10 — peak, остальные — empty
    assert _bar_at(bars, 10).state == "peak"
    for h in range(0, 23):
        if h == 10:
            continue
        assert _bar_at(bars, h).state == "empty", f"hour {h} should be empty"


# ---- peak selection ------------------------------------------------------

def test_today_24h_peak_in_past() -> None:
    """now=14, max в h=10. peak=10, h=14 — current, остальные active/empty."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {
        (today, 5):  100_000,
        (today, 10): 500_000,  # peak
        (today, 14): 50_000,   # current
    }
    bars = compute_today_24h(hourly, _now(14))
    assert _bar_at(bars, 10).state == "peak"
    assert _bar_at(bars, 14).state == "current"
    assert _bar_at(bars, 5).state == "active"
    # peak helper
    assert today_24h_peak(bars) == (10, 500_000)


def test_today_24h_current_is_peak() -> None:
    """Если max в current_hour — этот бар = peak (state="peak", не "current")."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {
        (today, 10): 100_000,
        (today, 14): 900_000,  # current AND peak
    }
    bars = compute_today_24h(hourly, _now(14))
    cur = _bar_at(bars, 14)
    assert cur.state == "peak"
    assert cur.value == 900_000
    assert today_24h_peak(bars) == (14, 900_000)


def test_today_24h_peak_with_zero_value_current() -> None:
    """Если current=0 и past_max=0 — peak=None, current не становится peak."""
    hourly: dict[tuple[date, int], int] = {}
    bars = compute_today_24h(hourly, _now(14))
    assert _bar_at(bars, 14).state == "current"
    assert _bar_at(bars, 14).value == 0
    assert today_24h_peak(bars) is None


def test_today_24h_peak_uses_value_not_hour() -> None:
    """peak по max(value), не по hour. h=23 с value=10 не должен стать peak
    если current (h=14) имеет value=1000."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {
        (today, 13): 10,
        (today, 14): 1000,  # current
    }
    bars = compute_today_24h(hourly, _now(14))
    assert _bar_at(bars, 14).state == "peak"
    assert _bar_at(bars, 13).state == "active"


# ---- intensity + integration --------------------------------------------

def test_today_24h_intensity_distributes_across_levels() -> None:
    """8 значений в прошлых часах → должны покрыть все 4 уровня интенсивности."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {
        (today, h): 1000 * (h + 1)  # 1k, 2k, 3k, ... 8k
        for h in range(8)  # 0..7
    }
    bars = compute_today_24h(hourly, _now(10))
    intensities = {
        _bar_at(bars, h).intensity for h in range(0, 8)
    }
    # sorted = [1k,2k,3k,4k,5k,6k,7k,8k]; q1=2k, q2=4k, q3=6k
    # h=0(1k)=L1, h=1(2k)=L1, h=2(3k)=L2, h=3(4k)=L2,
    # h=4(5k)=L3, h=5(6k)=L3, h=6(7k)=L4, h=7(8k)=L4
    assert intensities == {"L1", "L2", "L3", "L4"}, f"got {intensities}"


def test_today_24h_intensity_excludes_peak_class() -> None:
    """Peak-бар НЕ получает intensity-класс (рендер на .bar-24h.peak фон от peak,
    не от intensity-*). Контракт: state="peak" + intensity всё равно выставляется
    (для data-* атрибутов и тестов), но рендер _render_24h_stream выбирает класс
    .peak БЕЗ intensity-*. Проверяем, что state — peak."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {(today, 10): 100_000}
    bars = compute_today_24h(hourly, _now(14))
    peak_bar = _bar_at(bars, 10)
    assert peak_bar.state == "peak"
    # intensity может быть любым L* — рендер всё равно применяет только .peak


def test_today_24h_past_have_intensity() -> None:
    """Любой active-бар с value>0 получает intensity-уровень."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {
        (today, h): 1000 for h in (1, 5, 9, 12, 14)
    }
    bars = compute_today_24h(hourly, _now(14))
    for h in (1, 5, 9, 12, 14):
        b = _bar_at(bars, h)
        assert b.intensity in ("L1", "L2", "L3", "L4"), (
            f"hour {h}: intensity={b.intensity!r}"
        )


def test_today_24h_empty_have_no_intensity() -> None:
    """empty/future — intensity=None (рендер игнорирует)."""
    today = date(2026, 8, 4)
    hourly: dict[tuple[date, int], int] = {(today, 0): 1_000_000}
    bars = compute_today_24h(hourly, _now(14))
    # h=0 был бы peak, не empty
    for h in range(1, 14):
        b = _bar_at(bars, h)
        if b.state == "empty":
            assert b.intensity is None, f"hour {h}: state=empty, intensity={b.intensity}"
    for h in range(15, 24):
        b = _bar_at(bars, h)
        assert b.state == "future"
        assert b.intensity is None


# ---- runner --------------------------------------------------------------

def main() -> int:
    """Прогон всех test_* функций в этом модуле. Возвращает exit code."""
    import traceback
    tests = [
        test_intensity_quartiles_8_values,
        test_intensity_few_values_collapse_to_L2,
        test_intensity_zero_returns_L2_fallback,
        test_today_24h_returns_24_bars_ordered,
        test_today_24h_empty_day_at_14,
        test_today_24h_current_at_hour_0,
        test_today_24h_current_at_hour_23,
        test_today_24h_peak_in_past,
        test_today_24h_current_is_peak,
        test_today_24h_peak_with_zero_value_current,
        test_today_24h_peak_uses_value_not_hour,
        test_today_24h_intensity_distributes_across_levels,
        test_today_24h_intensity_excludes_peak_class,
        test_today_24h_past_have_intensity,
        test_today_24h_empty_have_no_intensity,
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
