"""Tests for hero-pill level logic and rendering.

Покрывает:
  - `pill_level`: границы 80% / 100%, edge cases (None/0/negative cap).
  - `_render_hero_pill`: markup с цветовым классом / без, скобочные суффиксы,
    fallback на "—" при None.
  - `_build_hero_pill_inner`: sep и paren_inline параметры.
  - `_render_combined_session_pill`: объединённый project | duration | ratio pill,
    bullet-сепаратор, inline-paren, эскейп path/title.

Запуск: `python tests/test_hero_pill.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    _build_hero_pill_inner,
    _render_combined_session_pill,
    _render_hero_pill,
    pill_level,
)


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


def test_render_pill_default_sep_is_slash() -> None:
    """По умолчанию sep='/' — backward-compat для day-pill'а (6.27M / 10.75M)."""
    html = _render_hero_pill(
        actual=100, actual_paren=None,
        cap=200, cap_paren=None,
        title="t",
    )
    assert 'hero-pill__sep">/</span>' in html
    # bullet НЕ должен быть в markup'е.
    assert "•" not in html


def test_render_pill_custom_sep() -> None:
    """Параметр sep пробрасывается в markup — нужно для combined-pill'а ('•')."""
    html = _render_hero_pill(
        actual=100, actual_paren=None,
        cap=200, cap_paren=None,
        title="t",
        sep="•",
    )
    # Аналогично test_render_pill_default_sep_is_slash: проверяем через
    # `>•</span>` (полный контент sep-элемента), а не `assert "•" in html`
    # (слишком широко — bullet может оказаться где-то ещё).
    assert 'hero-pill__sep">•</span>' in html
    # И что slash в sep-элементе больше нет — был заменён на bullet.
    assert 'hero-pill__sep">/</span>' not in html


# ---- _build_hero_pill_inner: paren_inline --------------------------------


def test_inner_paren_inline_nests_inside_value_span() -> None:
    """paren_inline=True → скобочный суффикс ВНУТРИ value-span'а, без flex-gap.

    Проверяем, что markup для actual выглядит как
    `<span class="hero-pill__actual …">273.8K<span class="hero-pill__paren">(5)</span></span>`,
    а НЕ как два отдельных flex-child'а (тогда был бы `>273.8K</span><span class="hero-pill__paren">`).
    """
    inner = _build_hero_pill_inner(
        actual=273_800, actual_paren=5,
        cap=877_500, cap_paren=5.7,
        sep="•",
        paren_inline=True,
    )
    # Оба paren'а вложены внутрь своих value-span'ов.
    assert '<span class="hero-pill__actual hero-pill__actual--ok">273.8K<span class="hero-pill__paren">(5)</span></span>' in inner
    assert '<span class="hero-pill__cap">877.5K<span class="hero-pill__paren">(5.7)</span></span>' in inner
    # Bullet между ними.
    assert 'hero-pill__sep">•<' in inner


def test_inner_paren_default_is_separate_child() -> None:
    """paren_inline=False (default) → paren отдельный flex-child.

    Проверяем, что скобка идёт ПОСЛЕ закрытия value-span'а, не внутри.
    Используется для standalone day-pill'а, который рендерится через
    `_render_hero_pill` (paren_inline по умолчанию False).
    """
    inner = _build_hero_pill_inner(
        actual=130_121, actual_paren=4,
        cap=306_239, cap_paren=4,
    )
    # value-span закрывается ДО paren'а (отдельные flex-child'ы).
    assert '<span class="hero-pill__actual hero-pill__actual--ok">130.1K</span><span class="hero-pill__paren">(4)</span>' in inner
    assert '<span class="hero-pill__cap">306.2K</span><span class="hero-pill__paren">(4)</span>' in inner


# ---- _render_combined_session_pill ----------------------------------------


def test_combined_pill_has_combined_class() -> None:
    """Outer span — `hero-pill hero-pill--combined`."""
    html = _render_combined_session_pill(
        title="agent-tokens-dashboard",
        duration_ms=28 * 60 * 1000,
        path="C:/Projects/Python/0803_agent-tokens-dashboard",
        actual=273_800, actual_paren=5,
        cap=877_500, cap_paren=5.7,
    )
    assert 'class="hero-pill hero-pill--combined"' in html


def test_combined_pill_contains_project_duration_ratio_in_order() -> None:
    """Структура: project → duration → ratio. Проверяем позиции в markup."""
    html = _render_combined_session_pill(
        title="agent-tokens-dashboard",
        duration_ms=28 * 60 * 1000,
        path="C:/x",
        actual=273_800, actual_paren=5,
        cap=877_500, cap_paren=5.7,
    )
    pos_project = html.index("agent-tokens-dashboard")
    pos_duration = html.index("28min")
    pos_actual = html.index("273.8K")
    pos_cap = html.index("877.5K")
    assert pos_project < pos_duration < pos_actual < pos_cap, (
        f"expected project<duration<actual<cap, got positions "
        f"{pos_project=}, {pos_duration=}, {pos_actual=}, {pos_cap=}"
    )


def test_combined_pill_uses_bullet_not_slash() -> None:
    """Ratio использует bullet '•' вместо '/'. TL-фидбэк 2026-08-05."""
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path="p",
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    # Bullet попадает в sep-span'е ratio-обёртки.
    # Не проверяем `assert "/" not in html` — `</span>` ломает (любой
    # закрывающий тег содержит '/'). Вместо этого ищем bullet в sep-span'е
    # внутри ratio-обёртки — уникальная позиция.
    ratio_start = html.index("hero-pill__ratio")
    assert 'hero-pill__sep">•</span>' in html[ratio_start:]


def test_combined_pill_paren_is_inline() -> None:
    """paren_inline=True → скобки внутри value-span'ов (плотный вид `273.8K(5)`).

    Это КЛЮЧЕВОЕ отличие от standalone session-pill'а: flex-gap родителя не
    разделяет число и скобки, иначе визуально `273.8K (5)` (с пробелом).
    """
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path="p",
        actual=273_800, actual_paren=5,
        cap=877_500, cap_paren=5.7,
    )
    assert '<span class="hero-pill__actual hero-pill__actual--ok">273.8K<span class="hero-pill__paren">(5)</span></span>' in html
    assert '<span class="hero-pill__cap">877.5K<span class="hero-pill__paren">(5.7)</span></span>' in html


def test_combined_pill_uses_ratio_wrapper() -> None:
    """Ratio-часть обёрнута в <span class="hero-pill__ratio"> для группы с
    собственной border-left (второй `|` после duration).

    Class-name `hero-pill__ratio` встречается ровно ОДИН раз — в открывающем
    теге (закрывающий — `</span>`, без class). Это и проверяем: ratio-wrapper
    реально оборачивает, а не случайный одиночный span.
    """
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path="p",
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    assert '<span class="hero-pill__ratio">' in html
    # Class-name только в opening tag → count == 1.
    assert html.count("hero-pill__ratio") == 1


def test_combined_pill_tooltip_combines_path_and_ratio_desc() -> None:
    """Tooltip — это path + '\\n\\n' + описание ratio (сохранено из старого session-pill'а)."""
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000,
        path="C:/Projects/Python/agent-tokens-dashboard",
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    assert 'title="C:/Projects/Python/agent-tokens-dashboard' in html
    assert "Токены в текущей сессии (запросы) / среднее по сессиям (запросы/сессию)" in html


def test_combined_pill_escapes_html_in_path_and_title() -> None:
    """XSS-защита: title и path экранируются перед вставкой в HTML.

    Если бы не escape, &, <, >, " в path/title ломали бы атрибут title= и
    позволили бы инъекцию markup'а. ВАЖНО: escape ровно один раз, иначе
    `&` от первого escape'а станет `&amp;` от второго (`&amp;quot;` вместо
    `&quot;`).
    """
    html = _render_combined_session_pill(
        title="<script>alert(1)</script>",
        duration_ms=60_000,
        path='C:/x"<y>&z',
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    # Сырые символы не должны появиться в markup'е.
    assert "<script>" not in html
    # Экранированные — должны (внутри .hero-pill__project и в title-атрибуте).
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # Кавычки в path экранируются (quote=True), но ровно один раз:
    # правильно `&quot;`, неправильно `&amp;quot;` (двойной escape).
    assert "&quot;" in html
    assert "&amp;quot;" not in html
    # Аналогично для <, >, & в path: одинарный escape.
    assert "&amp;lt;" not in html  # нет двойного escape для <
    assert "&amp;gt;" not in html  # нет двойного escape для >
    assert "&amp;amp;" not in html  # нет двойного escape для &


def test_combined_pill_handles_none_path() -> None:
    """path=None → в tooltip кладётся «путь не определён» (fallback для UI)."""
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path=None,
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    assert "путь не определён" in html


def test_combined_pill_handles_none_title_and_duration() -> None:
    """title=None → '—' в .hero-pill__project; duration_ms=None → '—' в .hero-pill__duration."""
    html = _render_combined_session_pill(
        title=None, duration_ms=None, path="p",
        actual=100, actual_paren=1,
        cap=200, cap_paren=2.0,
    )
    # fmt_duration(None) → '—' в duration.
    assert '<span class="hero-pill__duration">—</span>' in html
    # title=None → "—" в project (после html.escape).
    assert '<span class="hero-pill__project">—</span>' in html


def test_combined_pill_handles_none_actual_via_dash() -> None:
    """actual=None → fmt_tokens(None) = '—' в числителе, level='none' (без цвета)."""
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path="p",
        actual=None, actual_paren=None,
        cap=200, cap_paren=2.0,
    )
    assert '<span class="hero-pill__actual">—<' in html
    assert "hero-pill__actual--" not in html  # без цветового класса


def test_combined_pill_color_class_inherited_from_pill_level() -> None:
    """Числитель подсвечивается по pill_level: actual≤80% cap → ok (зелёный)."""
    # 80K / 200K = 40% → ok
    html = _render_combined_session_pill(
        title="x", duration_ms=60_000, path="p",
        actual=80_000, actual_paren=4,
        cap=200_000, cap_paren=4.0,
    )
    assert "hero-pill__actual--ok" in html


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
        test_render_pill_default_sep_is_slash,
        test_render_pill_custom_sep,
        # _build_hero_pill_inner
        test_inner_paren_inline_nests_inside_value_span,
        test_inner_paren_default_is_separate_child,
        # _render_combined_session_pill
        test_combined_pill_has_combined_class,
        test_combined_pill_contains_project_duration_ratio_in_order,
        test_combined_pill_uses_bullet_not_slash,
        test_combined_pill_paren_is_inline,
        test_combined_pill_uses_ratio_wrapper,
        test_combined_pill_tooltip_combines_path_and_ratio_desc,
        test_combined_pill_escapes_html_in_path_and_title,
        test_combined_pill_handles_none_path,
        test_combined_pill_handles_none_title_and_duration,
        test_combined_pill_handles_none_actual_via_dash,
        test_combined_pill_color_class_inherited_from_pill_level,
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
