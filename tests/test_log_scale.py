"""Tests for the linear/log scale toggle on the weekly chart.

Запускается напрямую: `python tests/test_log_scale.py`.
Тот же рантайм, что в tests/test_windows.py — без pytest.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Чтобы import работал и при запуске из корня, и из tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    Week,
    _y_ticks_for_log,
    _render_weekly_bars,
    WEEKDAY_LABELS,
)


def _make_weeks(specs: list[list[int | None]]) -> list[Week]:
    """Собрать Week-список из таблицы значений. specs[0] = самая старая."""
    out: list[Week] = []
    for i, days in enumerate(specs):
        out.append(
            Week(
                label=f"W-{30 + i}",
                monday=date(2026, 7, 27) if i == 0 else date(2026, 8, 3),  # упрощённо
                days=days,
                is_current=(i == len(specs) - 1),
            )
        )
    return out


def test_y_ticks_for_log_normal() -> None:
    """Реальный сценарий: max ~26M, min ~100K → 3 декады (10^5..10^8)."""
    weeks = _make_weeks([
        [8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000, 0],
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, None, None],
        [3_000_000, 1_500_000, 800_000, 2_500_000, 100_000, None, None],
    ])
    result = _y_ticks_for_log(weeks)
    assert result is not None, "expected ticks, got None"
    y_min, y_max, ticks = result
    # Headroom = +1 decade, поэтому max=10^8 даже при raw_max=26M.
    assert y_min == 10 ** 5, f"y_min={y_min}"
    assert y_max == 10 ** 8, f"y_max={y_max}"
    assert ticks == [10 ** 5, 10 ** 6, 10 ** 7, 10 ** 8], f"ticks={ticks}"


def test_y_ticks_for_log_all_none() -> None:
    """Всё None → None (caller падает обратно на linear-only)."""
    weeks = _make_weeks([
        [None] * 7,
        [None] * 7,
    ])
    assert _y_ticks_for_log(weeks) is None


def test_y_ticks_for_log_all_zero() -> None:
    """Всё 0 (без positive) → None."""
    weeks = _make_weeks([
        [0, 0, 0, 0, 0, 0, 0],
    ])
    assert _y_ticks_for_log(weeks) is None


def test_y_ticks_for_log_tight_range() -> None:
    """Узкий диапазон (<1 декады) → min 1 декады запаса."""
    weeks = _make_weeks([
        [1_000_000, 1_500_000, 1_200_000, 1_800_000, 2_000_000, 1_700_000, 1_900_000],
    ])
    result = _y_ticks_for_log(weeks)
    assert result is not None
    _, y_max, ticks = result
    # max=2M, exp_raw=6, +1 headroom → exp_max=7
    assert y_max == 10 ** 7
    assert len(ticks) >= 2, f"expected ≥2 ticks, got {ticks}"


def test_render_weekly_bars_linear_smoke() -> None:
    """Linear scale рендерит SVG без падения, ≥1 bar + labels."""
    weeks = _make_weeks([
        [8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, 11_000_000, 9_000_000],
    ])
    svg = _render_weekly_bars(weeks, "linear", 27_000_000)
    assert "W-30" in svg
    assert "W-31" in svg
    assert "26 000 000" in svg  # tick label
    assert "fill-orange-500" in svg  # palette class


def test_render_weekly_bars_log_smoke() -> None:
    """Log scale рендерит SVG, использует fmt_tokens для тиков."""
    weeks = _make_weeks([
        [8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, 11_000_000, 9_000_000],
    ])
    log_info = _y_ticks_for_log(weeks)
    assert log_info is not None
    svg = _render_weekly_bars(weeks, "log", log_info)
    # Тики лога должны быть в K/M формате.
    assert "10.00M" in svg or "100.00M" in svg, f"expected K/M tick label, got:\n{svg[:500]}"
    assert "log floor" not in svg  # все дни > 0


def test_render_weekly_bars_log_zero_day() -> None:
    """0-value день в log scale → floor bar (2px, disabled-color), не пропадает."""
    weeks = _make_weeks([
        [0, 8_000_000, 9_000_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
    ])
    log_info = _y_ticks_for_log(weeks)
    assert log_info is not None
    svg = _render_weekly_bars(weeks, "log", log_info)
    # Floor bar помечен "log floor" в title.
    assert "log floor" in svg, "expected log floor marker for 0-value day"
    # День-лейбл (Пн) всё равно рендерится.
    assert WEEKDAY_LABELS[0] in svg


def test_render_weekly_bars_log_none_day_unchanged() -> None:
    """None-day (no data) в log scale — тот же dashed placeholder, не floor bar."""
    weeks = _make_weeks([
        [None, 8_000_000, 9_000_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
    ])
    log_info = _y_ticks_for_log(weeks)
    assert log_info is not None
    svg = _render_weekly_bars(weeks, "log", log_info)
    assert "нет данных" in svg  # None-day title
    assert "log floor" not in svg  # 0-day не путаем с None


def test_persistence_script_in_dashboard_html() -> None:
    """Сгенерированный dashboard.html должен содержать localStorage-логику.

    Static check: проверяет, что ключевые куски inline-скрипта на месте.
    Ловит регрессию, если кто-то удалит/упростит toggle-скрипт и сломает
    persistence при <meta http-equiv="refresh"> и ребилде.
    """
    from build_dashboard import OUTPUT_PATH  # late import — нужен build
    if not OUTPUT_PATH.exists():
        # На свежем клоне dashboard.html ещё не сгенерирован — пропускаем.
        return
    html = OUTPUT_PATH.read_text(encoding="utf-8")
    must_contain = [
        "STORAGE_KEY",                  # константа для localStorage ключа
        "localStorage.getItem",         # чтение сохранённого значения
        "localStorage.setItem",         # запись при клике
        "tokenDashboardScale",          # имя ключа
        "try {",                        # защита от private mode / file://
    ]
    missing = [s for s in must_contain if s not in html]
    assert not missing, f"dashboard.html missing script pieces: {missing}"


def main() -> int:
    tests = [
        test_y_ticks_for_log_normal,
        test_y_ticks_for_log_all_none,
        test_y_ticks_for_log_all_zero,
        test_y_ticks_for_log_tight_range,
        test_render_weekly_bars_linear_smoke,
        test_render_weekly_bars_log_smoke,
        test_render_weekly_bars_log_zero_day,
        test_render_weekly_bars_log_none_day_unchanged,
        test_persistence_script_in_dashboard_html,
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
