"""Tests for the weekly cap threshold (PRD §6.5 / FR-8).

Покрывает чистую функцию `compute_weekly_threshold` и её интеграцию в
HTML-рендер `_render_weekly_grid`. Запускается без pytest, как и остальные
тесты в этой папке:

    python tests/test_weekly_cap.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# Чтобы import работал и при запуске из корня, и из tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    Week,
    WEEKLY_CAP_TOKENS,
    _render_weekly_grid,
    compute_weekly_threshold,
)


# ---- compute_weekly_threshold (чистая логика) -----------------------------
#
# Контракт (TL, 2026-08-16):
#   threshold = (cap − weekly_spent) / days_left
#   weekly_spent — суммарный расход от Пн до сегодня (включительно).
#   days_left — кол-во дней от сегодня до Вс включительно (Пн=7, Вс=1).
#   Это «средний лимит на каждый из оставшихся дней», чтобы уложиться в cap.


def test_threshold_monday_zero_spent() -> None:
    """Пн, потрачено 0 → cap / 7 = 8 571 428 (floor)."""
    # 60_000_000 / 7 = 8 571 428.57…, floor → 8 571 428
    assert compute_weekly_threshold(60_000_000, 0, 7) == 8_571_428


def test_threshold_monday_with_spent() -> None:
    """Пн, потрачено 2 430 000 → (60M − 2.43M) / 7 = 8 224 285.

    57 570 000 / 7 = 8 224 285.71…, floor → 8 224 285
    """
    assert compute_weekly_threshold(60_000_000, 2_430_000, 7) == 8_224_285


def test_threshold_sunday() -> None:
    """Вс, days_left=1 → (cap − weekly_spent) / 1 = cap − weekly_spent.

    (60 000 000 − 10 000 000) / 1 = 50 000 000
    """
    assert compute_weekly_threshold(60_000_000, 10_000_000, 1) == 50_000_000


def test_threshold_sunday_already_exhausted() -> None:
    """Вс, weekly_spent = cap → threshold = 0 (бюджет на оставшийся день = 0)."""
    assert compute_weekly_threshold(60_000_000, 60_000_000, 1) == 0


def test_threshold_exceeds_cap_returns_zero() -> None:
    """weekly_spent > cap → threshold = 0 (cap полностью превышена).

    Edge case: пользователь уже пробил 60M, дальше тратить нельзя.
    """
    assert compute_weekly_threshold(60_000_000, 70_000_000, 4) == 0


def test_threshold_days_left_zero_returns_none() -> None:
    """days_left <= 0 → None (защита от деления на 0, линия не рисуется).

    В реальной жизни такого не бывает (isoweekday ∈ [1..7] → days_left ∈ [1..7]),
    но контракт это явно фиксирует.
    """
    assert compute_weekly_threshold(60_000_000, 1_000_000, 0) is None
    assert compute_weekly_threshold(60_000_000, 1_000_000, -1) is None


def test_threshold_wednesday_midweek() -> None:
    """Ср, days_left=5, weekly_spent=5M → (60M − 5M) / 5 = 11M."""
    assert compute_weekly_threshold(60_000_000, 5_000_000, 5) == 11_000_000


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

    Текущая неделя привязана к реальному `date.today()` — её понедельник
    вычисляется динамически, чтобы day_d == today_d для одного из дней
    (этого требует условие рендера threshold-линии). Значение `today_value`
    кладётся именно в ЭТОТ день, остальные 6 — None.

    is_current_index — какой Week помечен как current (0=первый, 1=второй).
    """
    today_d = date.today()
    current_monday = today_d - timedelta(days=today_d.weekday())
    prev_monday = current_monday - timedelta(weeks=1)
    current_iso_week = current_monday.isocalendar().week
    prev_iso_week = prev_monday.isocalendar().week

    days: list[int | None] = [None] * 7
    days_split: list[tuple[int, int] | None] = [None] * 7
    if today_value is not None:
        days[today_d.weekday()] = today_value
        days_split[today_d.weekday()] = (0, today_value)

    return [
        Week(  # прошлая
            label=f"W-{prev_iso_week:02d}",
            monday=prev_monday,
            days=[5_000_000, 6_000_000, 7_000_000, 4_000_000, 5_500_000, 3_000_000, 4_500_000],
            # days_split — total кладём в output (input=0); эти тесты
            # проверяют threshold/render логику, split не валидируют.
            days_split=[(0, v) for v in [5_000_000, 6_000_000, 7_000_000,
                                          4_000_000, 5_500_000, 3_000_000, 4_500_000]],
            is_current=(is_current_index == 0),
        ),
        Week(  # текущая
            label=f"W-{current_iso_week:02d}",
            monday=current_monday,
            days=days,
            days_split=days_split,
            is_current=(is_current_index == 1),
        ),
    ]


def test_render_threshold_appears_on_current_day_only() -> None:
    """Лимит threshold рисуется ТОЛЬКО в текущей неделе, на сегодняшнем дне.

    В прошлой неделе порога быть не должно ни на одном дне.
    """
    # today=Пн, weekly_spent=2.43M, days_left=7 → 8.22M
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    current_label = weeks[1].label
    html = _render_weekly_grid(weeks, "linear", 60_000_000, weekly_threshold=8_224_285)

    # Threshold-блок присутствует
    assert 'class="threshold"' in html
    # Подпись со значением (label — только число, "средний лимит" живёт в title)
    assert "8.22M" in html

    # В прошлой неделе (всё, что до label текущей) порога нет
    prev_section = html.split(current_label)[0]
    assert 'class="threshold"' not in prev_section, "threshold не должен быть в прошлых неделях"


def test_render_threshold_omitted_when_none() -> None:
    """weekly_threshold=None → threshold-блок не рендерится вообще."""
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 60_000_000, weekly_threshold=None)
    assert 'class="threshold"' not in html
    assert "порог" not in html


def test_render_threshold_omitted_when_today_is_none() -> None:
    """Если за сегодня ещё нет данных (today_value=None), но threshold всё равно
    передаётся — главное чтобы он не сломал рендер и попал на бар.

    На практике main() передаст weekly_spent=0, не None, но проверим,
    что рендер устойчив к граничному входу (today=None → bar future, threshold
    внутри bar-cell всё равно отрендерится, потому что day_d == today_d).
    """
    weeks = _make_weeks_for_render(today_value=None, is_current_index=1)
    # Не падает, threshold-блок может быть (т.к. day_d == today_d всё равно верно),
    # но это редкий сценарий — главное, что не падает.
    html = _render_weekly_grid(weeks, "linear", 60_000_000, weekly_threshold=8_000_000)
    # bar.future (т.к. value is None), но .bar-cell всё равно есть
    assert 'class="bar-cell"' in html


def test_render_threshold_positioned_via_bottom_pct() -> None:
    """Threshold прибит к шкале тем же процентом, что высота бара с value=threshold.

    Проверяем: bottom:N% присутствует в HTML (значит CSS-позиционирование сработает).
    """
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 60_000_000, weekly_threshold=8_224_285)
    assert 'class="threshold" style="bottom:' in html


def test_render_bar_cell_wraps_each_bar() -> None:
    """Все 14 баров (2 недели × 7 дней) обёрнуты в .bar-cell.

    Это регрессионный тест на изменение DOM-структуры: до порога .bar был
    прямым flex-child .bars, после — обёрнут в .bar-cell для absolute-позиционирования.
    """
    weeks = _make_weeks_for_render(today_value=2_430_000, is_current_index=1)
    html = _render_weekly_grid(weeks, "linear", 60_000_000, weekly_threshold=8_224_285)
    # 14 .bar-cell обёрток
    assert html.count('class="bar-cell"') == 14, (
        f"expected 14 bar-cells, got {html.count('class=\"bar-cell\"')}"
    )


# ---- default constant sanity --------------------------------------------


def test_weekly_cap_default_is_60m() -> None:
    """Защита от случайной правки дефолта в build_dashboard.py.

    Если кто-то поменяет 60_000_000 на другое число, тест напомнит —
    это бизнес-параметр, который согласован в PRD §6.5.
    """
    assert WEEKLY_CAP_TOKENS == 60_000_000


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
        test_weekly_cap_default_is_60m,
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
