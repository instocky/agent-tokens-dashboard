"""Tests for the weekly cap threshold (PRD §6.5 / FR-8).

Покрывает чистую функцию `compute_weekly_threshold` и её интеграцию в
HTML-рендер `_render_weekly_grid`. Запускается без pytest, как и остальные
тесты в этой папке:

    python tests/test_weekly_cap.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Чтобы import работал и при запуске из корня, и из tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import (  # noqa: E402
    Week,
    WEEKLY_CAP_TOKENS,
    compute_weekly_threshold,
)
from build_dashboard import _render_weekly_grid  # noqa: E402  (Step 2: render)


# ---- compute_weekly_threshold (чистая логика) -----------------------------


def test_threshold_monday_zero_spent() -> None:
    """Пн, потрачено 0 → cap / 7 = 10 714 285 (floor)."""
    # 75_000_000 / 7 = 10 714 285.71…, floor → 10 714 285
    assert compute_weekly_threshold(75_000_000, 0, 7) == 10_714_285


def test_threshold_monday_with_spent() -> None:
    """Пн, потрачено 2 430 000 (как в скриншоте) → (75M − 2.43M) / 7 = 10 367 142.

    72 570 000 / 7 = 10 367 142.85…, floor → 10 367 142
    """
    assert compute_weekly_threshold(75_000_000, 2_430_000, 7) == 10_367_142


def test_threshold_sunday() -> None:
    """Вс, days_left=1 → cap − today_spent, без деления.

    (75 000 000 − 60 000 000) / 1 = 15 000 000
    """
    assert compute_weekly_threshold(75_000_000, 60_000_000, 1) == 15_000_000


def test_threshold_sunday_already_exhausted() -> None:
    """Вс, потрачено 75M (вся капа) → threshold = 0."""
    assert compute_weekly_threshold(75_000_000, 75_000_000, 1) == 0


def test_threshold_exceeds_cap_returns_zero() -> None:
    """today_spent > cap → threshold = 0 (cap полностью превышена).

    Это edge case: пользователь уже пробил 75M, дальше тратить нельзя.
    """
    assert compute_weekly_threshold(75_000_000, 80_000_000, 4) == 0


def test_threshold_days_left_zero_returns_none() -> None:
    """days_left <= 0 → None (защита от деления на 0, линия не рисуется).

    В реальной жизни такого не бывает (isoweekday ∈ [1..7] → days_left ∈ [1..7]),
    но контракт это явно фиксирует.
    """
    assert compute_weekly_threshold(75_000_000, 1_000_000, 0) is None
    assert compute_weekly_threshold(75_000_000, 1_000_000, -1) is None


def test_threshold_wednesday_midweek() -> None:
    """Ср, days_left=5, потрачено 5M → (75M − 5M) / 5 = 14M."""
    assert compute_weekly_threshold(75_000_000, 5_000_000, 5) == 14_000_000


def test_threshold_floor_not_ceil() -> None:
    """Floor вниз: 100 / 3 = 33.33, должно быть 33, не 34.

    Контракт: «лучше показать заниженный порог, чем подтолкнуть к превышению».
    """
    assert compute_weekly_threshold(100, 0, 3) == 33


def test_threshold_zero_cap_returns_zero() -> None:
    """cap = 0 → threshold = 0 (вырожденный случай, без падения)."""
    assert compute_weekly_threshold(0, 0, 7) == 0
    assert compute_weekly_threshold(0, 100, 7) == 0


# ---- _render_weekly_grid (интеграция в HTML) ------------------------------


def _make_weeks_for_render(
    today_value: int | None,
    is_current_index: int = 0,
) -> list[Week]:
    """Собрать минимальный Week-список: одна прошлая + одна текущая.

    В текущей неделе Пн=today_value, остальные None (не мешают тесту).
    is_current_index — какой Week помечен как current (0=первый, 1=второй).
    """
    return [
        Week(  # прошлая
            label="W-31",
            monday=date(2026, 7, 27),
            days=[5_000_000, 6_000_000, 7_000_000, 4_000_000, 5_500_000, 3_000_000, 4_500_000],
            is_current=(is_current_index == 0),
        ),
        Week(  # текущая
            label="W-32",
            monday=date(2026, 8, 3),
            days=[today_value, None, None, None, None, None, None],
            is_current=(is_current_index == 1),
        ),
    ]


def test_render_threshold_appears_on_current_day_only() -> None:
    """Лимит threshold рисуется ТОЛЬКО в W-32 (current), Пн (today).

    В прошлой W-31 порога быть не должно ни на одном дне.
    """
    # Пн W-32, потрачено 2.43M, days_left=7 → 10.37M
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 75_000_000, weekly_threshold=10_367_142)

    # Threshold-блок присутствует
    assert 'class="threshold"' in html
    # Подпись со значением (label — только число, "порог" живёт в легенде)
    assert "10.37M" in html

    # В W-31 порога нет (count() == 1 — только в W-32)
    w31_section = html.split('W-32')[0]
    assert 'class="threshold"' not in w31_section, "threshold не должен быть в прошлых неделях"


def test_render_threshold_omitted_when_none() -> None:
    """weekly_threshold=None → threshold-блок не рендерится вообще."""
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 75_000_000, weekly_threshold=None)
    assert 'class="threshold"' not in html
    assert "порог" not in html


def test_render_threshold_omitted_when_today_is_none() -> None:
    """Если за сегодня ещё нет данных (today_value=None), но threshold всё равно
    передаётся — главное чтобы он не сломал рендер и попал на бар.

    На практике main() передаст today_spent=0, не None, но проверим,
    что рендер устойчив к граничному входу (today=None → bar future, threshold
    внутри bar-cell всё равно отрендерится, потому что day_d == today_d).
    """
    weeks = _make_weeks_for_render(today_value=None, is_current_index=1)
    # Не падает, threshold-блок может быть (т.к. day_d == today_d всё равно верно),
    # но это редкий сценарий — главное, что не падает.
    html = _render_weekly_grid(weeks, "linear", 75_000_000, weekly_threshold=10_000_000)
    # bar.future (т.к. value is None), но .bar-cell всё равно есть
    assert 'class="bar-cell"' in html


def test_render_threshold_positioned_via_bottom_pct() -> None:
    """Threshold прибит к шкале тем же процентом, что высота бара с value=threshold.

    Проверяем: bottom:N% присутствует в HTML (значит CSS-позиционирование сработает).
    """
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 75_000_000, weekly_threshold=10_367_142)
    assert 'class="threshold" style="bottom:' in html


def test_render_bar_cell_wraps_each_bar() -> None:
    """Все 14 баров (2 недели × 7 дней) обёрнуты в .bar-cell.

    Это регрессионный тест на изменение DOM-структуры: до порога .bar был
    прямым flex-child .bars, после — обёрнут в .bar-cell для absolute-позиционирования.
    """
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 75_000_000, weekly_threshold=10_367_142)
    # 14 .bar-cell обёрток
    assert html.count('class="bar-cell"') == 14, (
        f"expected 14 bar-cells, got {html.count('class=\"bar-cell\"')}"
    )


# ---- default constant sanity --------------------------------------------


def test_weekly_cap_default_is_75m() -> None:
    """Защита от случайной правки дефолта в build_dashboard.py.

    Если кто-то поменяет 75_000_000 на другое число, тест напомнит —
    это бизнес-параметр, который согласован в PRD §6.5.
    """
    assert WEEKLY_CAP_TOKENS == 75_000_000


# ---- main ----------------------------------------------------------------


def main() -> int:
    tests = [
        # compute_weekly_threshold
        test_threshold_monday_zero_spent,
        test_threshold_monday_with_spent,
        test_threshold_sunday,
        test_threshold_sunday_already_exhausted,
        test_threshold_exceeds_cap_returns_zero,
        test_threshold_days_left_zero_returns_none,
        test_threshold_wednesday_midweek,
        test_threshold_floor_not_ceil,
        test_threshold_zero_cap_returns_zero,
        # _render_weekly_grid
        test_render_threshold_appears_on_current_day_only,
        test_render_threshold_omitted_when_none,
        test_render_threshold_omitted_when_today_is_none,
        test_render_threshold_positioned_via_bottom_pct,
        test_render_bar_cell_wraps_each_bar,
        # default
        test_weekly_cap_default_is_75m,
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
