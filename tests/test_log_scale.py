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
    _render_weekly_grid,
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


def test_render_weekly_grid_linear_smoke() -> None:
    """Linear: HTML-грид с .week / .bar.history / .bar.future классами."""
    weeks = _make_weeks([
        [8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, 11_000_000, 9_000_000],
    ])
    html = _render_weekly_grid(weeks, "linear", 27_000_000)
    assert "W-30" in html
    assert "W-31" in html
    assert 'class="week' in html
    assert 'class="bar history"' in html
    # 7 дней × 2 недели = 14 баров с height:NN.N%
    assert html.count('style="height:') == 14
    # Лейблы дней под барами
    for label in WEEKDAY_LABELS:
        assert f"<span>{label}</span>" in html


def test_render_weekly_grid_log_smoke() -> None:
    """Log: те же классы, но высоты считаются по log-шкале."""
    weeks = _make_weeks([
        [8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, 11_000_000, 9_000_000],
    ])
    log_info = _y_ticks_for_log(weeks)
    assert log_info is not None
    html = _render_weekly_grid(weeks, "log", log_info)
    # В log шкале диапазон 9M..26M сжимается (оба в одной декаде),
    # поэтому 9M (второй бар) должен быть ВЫШЕ, чем в linear (где 26M = 100%).
    linear_html = _render_weekly_grid(weeks, "linear", 27_000_000)
    import re
    def _h(s: str, day: str) -> float:
        m = re.search(rf'class="bar (?:history|accent)" style="height:([\d.]+)%" title="[^"]*, {day}: ', s)
        return float(m.group(1)) if m else -1.0
    # Вторник W-30 = 9M
    assert _h(html, "Вт") > _h(linear_html, "Вт"), "log должен сжимать диапазон, делая нижние значения выше"


def test_render_weekly_grid_current_week_accent() -> None:
    """Current week → class 'week current' (is_current=True), past weeks — 'week'."""
    weeks = _make_weeks([
        [8_000_000] * 7,  # past
        [9_000_000] * 7,  # current (is_current=True)
    ])
    html = _render_weekly_grid(weeks, "linear", 10_000_000)
    assert 'class="week current"' in html
    assert html.count('class="week current"') == 1


def test_render_weekly_grid_none_day_title() -> None:
    """None-day → bar future с title 'нет данных'."""
    weeks = _make_weeks([
        [None, 8_000_000, 9_000_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
    ])
    html = _render_weekly_grid(weeks, "linear", 20_000_000)
    assert 'class="bar future"' in html
    assert "нет данных" in html
    # 0-day НЕ должен получить future-стиль; это history с height=0%.
    weeks2 = _make_weeks([
        [0, 8_000_000, 9_000_000, 11_000_000, 12_000_000, 16_000_000, 17_000_000],
    ])
    html2 = _render_weekly_grid(weeks2, "linear", 20_000_000)
    # Понедельник с 0 → history (не future), height=0% (CSS min-height даст 6px)
    assert 'class="bar history" style="height:0.0%"' in html2
    assert 'W-30, Пн: 0' in html2


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
        test_render_weekly_grid_linear_smoke,
        test_render_weekly_grid_log_smoke,
        test_render_weekly_grid_current_week_accent,
        test_render_weekly_grid_none_day_title,
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
