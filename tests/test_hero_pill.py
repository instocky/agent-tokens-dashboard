"""Tests for hero-pill level logic and rendering.

Покрывает:
  - `pill_level`: границы 80% / 100%, edge cases (None/0/negative cap).
  - `_render_hero_pill`: markup с цветовым классом / без, скобочные суффиксы,
    fallback на "—" при None.

Запуск: `python tests/test_hero_pill.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import _render_hero_pill, pill_level  # noqa: E402


# ---- pill_level: чистая логика ---------------------------------------------


def test_pill_level_ok_below_80pct() -> None:
    """≤80% → ok (зелёный). Пример: 130/306 = 42.5%."""
    assert pill_level(130_000, 306_000) == "ok"


def test_pill_level_ok_at_80pct_boundary() -> None:
    """Ровно 80% — ещё ok (порог строгий: 80% < pct ≤ 100% = warn)."""
    assert pill_level(80, 100) == "ok"


def test_pill_level_warn_above_80pct() -> None:
    """80% < pct ≤ 100% → warn (оранжевый). 90/100 = 90%."""
    assert pill_level(90, 100) == "warn"


def test_pill_level_warn_at_100pct_boundary() -> None:
    """Ровно 100% — ещё warn (порог: >1.0 = over)."""
    assert pill_level(100, 100) == "warn"


def test_pill_level_over_above_100pct() -> None:
    """>100% → over (красный). 130/100 = 130%."""
    assert pill_level(130, 100) == "over"


def test_pill_level_none_when_actual_zero() -> None:
    """actual == 0 → none. День ещё не начался / сессия пустая — нейтрально,
    не "ok" (0% от cap — это не "зелёный сигнал", это "ничего не сожгли")."""
    assert pill_level(0, 14_000_000) == "none"


def test_pill_level_none_when_cap_zero() -> None:
    """cap == 0 → none. Нечего с чем сравнивать."""
    assert pill_level(1000, 0) == "none"


def test_pill_level_none_when_cap_negative() -> None:
    """cap < 0 (вырожденный вход) → none. Защита от деления на отрицательное."""
    assert pill_level(1000, -1) == "none"


def test_pill_level_none_when_actual_none() -> None:
    """actual == None → none. Сессия не началась / нет данных."""
    assert pill_level(None, 1000) == "none"


def test_pill_level_none_when_cap_none() -> None:
    """cap == None → none. Threshold не вычислился (days_left=0)."""
    assert pill_level(1000, None) == "none"


# ---- _render_hero_pill: markup ---------------------------------------------


def test_render_pill_ok_has_green_class() -> None:
    """actual ≤ 80% от cap → класс .hero-pill__actual--ok (зелёный)."""
    html = _render_hero_pill(
        actual=130_121, actual_paren=4,
        cap=306_239, cap_paren=4.0,
        title="test",
    )
    assert "hero-pill__actual--ok" in html
    assert "hero-pill__actual--warn" not in html
    assert "hero-pill__actual--over" not in html
    # Числитель отформатирован (fmt_tokens округляет до 1 знака)
    assert ">130.1K<" in html
    # Знаменатель — белый, без цветового класса
    assert 'hero-pill__cap">306.2K<' in html
    # Скобочные суффиксы
    assert 'hero-pill__paren">(4)<' in html


def test_render_pill_warn_has_orange_class() -> None:
    """80% < actual ≤ 100% → .hero-pill__actual--warn."""
    html = _render_hero_pill(
        actual=90, actual_paren=None,
        cap=100, cap_paren=None,
        title="t",
    )
    assert "hero-pill__actual--warn" in html
    assert "hero-pill__actual--ok" not in html
    assert "hero-pill__actual--over" not in html


def test_render_pill_over_has_red_class() -> None:
    """actual > 100% → .hero-pill__actual--over."""
    html = _render_hero_pill(
        actual=130, actual_paren=None,
        cap=100, cap_paren=None,
        title="t",
    )
    assert "hero-pill__actual--over" in html
    assert "hero-pill__actual--ok" not in html
    assert "hero-pill__actual--warn" not in html


def test_render_pill_none_actual_no_color_class() -> None:
    """actual == 0 или None → без цветового класса (нейтральный)."""
    html = _render_hero_pill(
        actual=0, actual_paren=None,
        cap=1000, cap_paren=None,
        title="t",
    )
    assert "hero-pill__actual--" not in html
    # fmt_tokens(0) → "0", не "—"
    assert ">0<" in html


def test_render_pill_none_cap_renders_dash() -> None:
    """cap == None → знаменатель "—" (как fmt_tokens для None)."""
    html = _render_hero_pill(
        actual=1000, actual_paren=None,
        cap=None, cap_paren=None,
        title="t",
    )
    assert "hero-pill__cap\">—<" in html
    # Числитель без цвета, т.к. cap=None → level="none"
    assert "hero-pill__actual--" not in html


def test_render_pill_no_paren_when_value_none() -> None:
    """paren=None → блок .hero-pill__paren не рендерится вообще."""
    html = _render_hero_pill(
        actual=1000, actual_paren=None,
        cap=2000, cap_paren=None,
        title="t",
    )
    assert "hero-pill__paren" not in html


def test_render_pill_paren_int_vs_float() -> None:
    """paren=int → без .0; paren=float дробный → через fmt_avg (один знак)."""
    html_int = _render_hero_pill(
        actual=100, actual_paren=4,
        cap=200, cap_paren=4,
        title="t",
    )
    assert 'hero-pill__paren">(4)<' in html_int

    html_float = _render_hero_pill(
        actual=100, actual_paren=3.7,
        cap=200, cap_paren=4.7,
        title="t",
    )
    assert 'hero-pill__paren">(3.7)<' in html_float
    assert 'hero-pill__paren">(4.7)<' in html_float


def test_render_pill_title_attr_present() -> None:
    """Tooltip из title попадает в атрибут title=. валидный HTML."""
    html = _render_hero_pill(
        actual=100, actual_paren=None,
        cap=200, cap_paren=None,
        title="Текущая / средняя",
    )
    assert 'title="Текущая / средняя"' in html


# ---- runner ----------------------------------------------------------------


def main() -> int:
    tests = [
        # pill_level
        test_pill_level_ok_below_80pct,
        test_pill_level_ok_at_80pct_boundary,
        test_pill_level_warn_above_80pct,
        test_pill_level_warn_at_100pct_boundary,
        test_pill_level_over_above_100pct,
        test_pill_level_none_when_actual_zero,
        test_pill_level_none_when_cap_zero,
        test_pill_level_none_when_cap_negative,
        test_pill_level_none_when_actual_none,
        test_pill_level_none_when_cap_none,
        # _render_hero_pill
        test_render_pill_ok_has_green_class,
        test_render_pill_warn_has_orange_class,
        test_render_pill_over_has_red_class,
        test_render_pill_none_actual_no_color_class,
        test_render_pill_none_cap_renders_dash,
        test_render_pill_no_paren_when_value_none,
        test_render_pill_paren_int_vs_float,
        test_render_pill_title_attr_present,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
