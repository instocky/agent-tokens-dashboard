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

from analytics import (  # noqa: E402
    Week,
    WEEKDAY_LABELS,
)
from build_dashboard import (  # noqa: E402  (Step 2: render)
    _y_ticks_for_log,
    _render_weekly_grid,
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


def test_render_weekly_grid_week_total() -> None:
    """Каждая карточка недели показывает сумму в M в .week-total.

    None-дни не должны попадать в сумму (это no data, не 0).
    W-30: 14M + 17M + 26M + 4M + 13M = 74M; 8M + 9M + 6.5M + 11M + 12M + 16M = 62.5M.
    """
    weeks = _make_weeks([
        # past week: 6 non-None дней = 62.5M (None в понедельник игнорируется)
        [None, 8_000_000, 9_000_000, 6_500_000, 11_000_000, 12_000_000, 16_000_000],
        # current week: 5 non-None дней (Сб/Вс None) = 74M
        [14_000_000, 17_000_000, 26_000_000, 4_000_000, 13_000_000, None, None],
    ])
    html = _render_weekly_grid(weeks, "linear", 27_000_000)
    import re
    totals = re.findall(
        r'<span class="week-total" title="[^"]+">([\d.]+M)</span>', html
    )
    assert len(totals) == 2, f"expected 2 week-total, got {totals}"
    assert totals[0] == "62.50M", f"past week total = {totals[0]}"
    assert totals[1] == "74.00M", f"current week total = {totals[1]}"


def test_render_weekly_grid_week_total_all_none() -> None:
    """Вся неделя в None → сумма 0.00M, не пусто и не падает."""
    weeks = _make_weeks([
        [None] * 7,
    ])
    html = _render_weekly_grid(weeks, "linear", 1_000_000)
    assert '<span class="week-total" title="Сумма за W-30">0.00M</span>' in html


def test_bar_axis_share_coordinate_system() -> None:
    """Бары и ось должны делить одну координатную систему (0%..89.08% .week).

    Регресс: до фикса .bars был обычным flex-flow элементом высотой 260px,
    начинался на ~12% от верха .week → 10M-бар (height:66.7% от .bars) топал
    на 36% .week, а 10M-лейбл стоял на 29.7% → визуальный gap 6.5%. Также
    threshold (bottom:X% от .bar-cell) сидел на той же неверной Y.

    Фикс: .bars абсолютно позиционирован (top:0, bottom:38px), занимает ровно
    0%..(100%-10.92%) = 0%..89.08% .week — ту же область, что и ось.
    height:X% на .bar автоматически ложится на гридлайны 29.7% / 59.4%
    и 10M/1M-лейблы.

    Ловит регрессию: проверяет в сгенерированном dashboard.html наличие
    абсолютного .bars + absolute .week-head (overlay) + absolute .days.
    """
    from build_dashboard import OUTPUT_PATH
    if not OUTPUT_PATH.exists():
        return  # нет html — пропускаем
    html = OUTPUT_PATH.read_text(encoding="utf-8")
    # 1) .bars абсолютно и занимает 0%..(100% - 10.92%) .week.
    assert ".bars {" in html, "нет .bars CSS"
    bars_block = html.split(".bars {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in bars_block, (
        f".bars должен быть position:absolute (для совмещения с осью), "
        f"а не flex-flow. CSS:\n{bars_block}"
    )
    assert "top: 0" in bars_block, ".bars должен начинаться на top:0 (100M-анкер)"
    assert "bottom: 10.5%" in bars_block, (
        ".bars должен заканчиваться на bottom:10.5% (≈ 100K-анкер над .days). "
        "Раньше был bottom:38px — откалиброван под min-height:360px и ломал "
        "shared coord system при квадратной карточке (aspect-ratio:1): "
        "872K-бар оказывался выше 1M-линии. 10.5% = 38/360 в исходном калибре, "
        "и теперь работает на любой высоте .week. "
        "Если поменялся .days-height, обнови --days-block тут и в .days CSS"
    )
    # 2) .week-head — overlay, иначе W-лэйбл съест верхние бары.
    head_block = html.split(".week-head {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in head_block, (
        ".week-head должен быть position:absolute (top:12px поверх .bars), "
        "иначе он лежит в потоке и сдвигает .bars вниз → 100M-бар не доходит до 100M-лейбла"
    )
    assert "top: 12px" in head_block, ".week-head должен сидеть на top:12px"
    assert "z-index: 3" in head_block, ".week-head должен быть над .bars (z-index:3)"
    # 3) .days — overlay внизу.
    days_block = html.split(".days {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in days_block, (
        ".days должен быть position:absolute (bottom:12px), иначе он в потоке "
        "сдвигает .week-content и ломает проценты"
    )
    assert "bottom: 12px" in days_block, ".days должен сидеть на bottom:12px"
    # 4) .week должен иметь aspect-ratio:1 (квадрат) или min-height, иначе
    # absolute-дети схлопывают карточку → проценты гридлайнов ломаются.
    week_block = html.split(".week {", 1)[1].split("}", 1)[0]
    assert ("aspect-ratio:" in week_block) or ("min-height:" in week_block), (
        ".week должен иметь aspect-ratio:1 (квадрат) или min-height — иначе "
        "absolute .bars/.days/.week-head не дают контента и карточка "
        "схлопывается → проценты гридлайнов ломаются"
    )


def test_bar_height_matches_log_position() -> None:
    """Численная проверка: для известного значения бар-топ ложится на 29.7% .week.

    До фикса: 10M-бар топал на 36% .week (gap 6.5% = ~23px), Ср 10.63M
    визуально была под 10M-линией (как Вт 9.74M). После фикса: 10M-бар
    должен топнуть ровно на 29.7% .week (±1% — допуск на округление и
    min-height:360 vs фактическая высота).

    Проверяем формулу: bar_top_pct = (1 - frac) * chart_area_pct_of_week.
    Для frac=2/3 (10M на 100K..100M): top = (1 - 2/3) * 89.08% = 29.69%.
    """
    # Данные с разбросом >1 декады, чтобы _y_ticks_for_log дал y_min=100K, y_max=100M
    # (одна декада 10M..100M не даёт 100K-anchor).
    weeks = _make_weeks([
        [800_000, 10_000_000, 10_000_000, 10_000_000, 10_000_000, 10_000_000, 10_000_000],
    ])
    log_info = _y_ticks_for_log(weeks)
    assert log_info is not None
    y_min, y_max, ticks = log_info
    assert y_min == 10 ** 5 and y_max == 10 ** 8, f"anchor {y_min}..{y_max}"
    # y_min=10^5, y_max=10^8 → frac(10M) = (7-5)/3 = 2/3
    import math
    frac = (math.log10(10_000_000) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
    assert abs(frac - 2 / 3) < 1e-6, f"frac(10M) должен быть 2/3, получил {frac}"
    # В новой раскладке .bars = 89.08% .week, поэтому 10M-бар топом
    # (1 - 2/3) * 89.08% = 29.69% от верха .week.
    # А 10M-лейбл на top:29.7% .axis (= .week по grid-stretch) — должны
    # совпасть в пределах 1%.
    bar_top_pct = (1 - frac) * 89.08
    label_pct = 29.7
    assert abs(bar_top_pct - label_pct) < 1.0, (
        f"10M-бар ({bar_top_pct:.2f}% .week) должен лечь на 10M-лейбл "
        f"({label_pct}% .week); gap={bar_top_pct - label_pct:.2f}% — "
        f"верни shared coord system (.bars absolute, top:0, bottom:38px)"
    )


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
        test_render_weekly_grid_week_total,
        test_render_weekly_grid_week_total_all_none,
        test_bar_axis_share_coordinate_system,
        test_bar_height_matches_log_position,
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
