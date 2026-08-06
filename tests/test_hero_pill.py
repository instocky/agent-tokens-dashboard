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
from analytics import pill_level
from render_dashboard import _build_hero_pill_inner, _render_combined_session_pill, _render_hero_pill

def test_pill_level_ok_below_80pct() -> None:
    """≤80% → ok (зелёный). Пример: 130/306 = 42.5%."""
    assert pill_level(130000, 306000) == 'ok'

def test_pill_level_ok_at_80pct_boundary() -> None:
    """Ровно 80% — ещё ok (порог строгий: 80% < pct ≤ 100% = warn)."""
    assert pill_level(80, 100) == 'ok'

def test_pill_level_warn_above_80pct() -> None:
    """80% < pct ≤ 100% → warn (оранжевый). 90/100 = 90%."""
    assert pill_level(90, 100) == 'warn'

def test_pill_level_warn_at_100pct_boundary() -> None:
    """Ровно 100% — ещё warn (порог: >1.0 = over)."""
    assert pill_level(100, 100) == 'warn'

def test_pill_level_over_above_100pct() -> None:
    """>100% → over (красный). 130/100 = 130%."""
    assert pill_level(130, 100) == 'over'

def test_pill_level_none_when_actual_zero() -> None:
    """actual == 0 → none. День ещё не начался / сессия пустая — нейтрально,
    не "ok" (0% от cap — это не "зелёный сигнал", это "ничего не сожгли")."""
    assert pill_level(0, 14000000) == 'none'

def test_pill_level_none_when_cap_zero() -> None:
    """cap == 0 → none. Нечего с чем сравнивать."""
    assert pill_level(1000, 0) == 'none'

def test_pill_level_none_when_cap_negative() -> None:
    """cap < 0 (вырожденный вход) → none. Защита от деления на отрицательное."""
    assert pill_level(1000, -1) == 'none'

def test_pill_level_none_when_actual_none() -> None:
    """actual == None → none. Сессия не началась / нет данных."""
    assert pill_level(None, 1000) == 'none'

def test_pill_level_none_when_cap_none() -> None:
    """cap == None → none. Threshold не вычислился (days_left=0)."""
    assert pill_level(1000, None) == 'none'

def test_render_pill_ok_has_green_class() -> None:
    """actual ≤ 80% от cap → класс .hero-pill__actual--ok (зелёный)."""
    html = _render_hero_pill(actual=130121, actual_paren=4, cap=306239, cap_paren=4.0, title='test', level=pill_level(130121, 306239))
    assert 'hero-pill__actual--ok' in html
    assert 'hero-pill__actual--warn' not in html
    assert 'hero-pill__actual--over' not in html
    assert '>130.1K<' in html
    assert 'hero-pill__cap">306.2K<' in html
    assert 'hero-pill__paren">(4)<' in html

def test_render_pill_warn_has_orange_class() -> None:
    """80% < actual ≤ 100% → .hero-pill__actual--warn."""
    html = _render_hero_pill(actual=90, actual_paren=None, cap=100, cap_paren=None, title='t', level=pill_level(90, 100))
    assert 'hero-pill__actual--warn' in html
    assert 'hero-pill__actual--ok' not in html
    assert 'hero-pill__actual--over' not in html

def test_render_pill_over_has_red_class() -> None:
    """actual > 100% → .hero-pill__actual--over."""
    html = _render_hero_pill(actual=130, actual_paren=None, cap=100, cap_paren=None, title='t', level=pill_level(130, 100))
    assert 'hero-pill__actual--over' in html
    assert 'hero-pill__actual--ok' not in html
    assert 'hero-pill__actual--warn' not in html

def test_render_pill_none_actual_no_color_class() -> None:
    """actual == 0 или None → без цветового класса (нейтральный)."""
    html = _render_hero_pill(actual=0, actual_paren=None, cap=1000, cap_paren=None, title='t', level=pill_level(0, 1000))
    assert 'hero-pill__actual--' not in html
    assert '>0<' in html

def test_render_pill_none_cap_renders_dash() -> None:
    """cap == None → знаменатель "—" (как fmt_tokens для None)."""
    html = _render_hero_pill(actual=1000, actual_paren=None, cap=None, cap_paren=None, title='t', level=pill_level(1000, None))
    assert 'hero-pill__cap">—<' in html
    assert 'hero-pill__actual--' not in html

def test_render_pill_no_paren_when_value_none() -> None:
    """paren=None → блок .hero-pill__paren не рендерится вообще."""
    html = _render_hero_pill(actual=1000, actual_paren=None, cap=2000, cap_paren=None, title='t', level=pill_level(1000, 2000))
    assert 'hero-pill__paren' not in html

def test_render_pill_paren_int_vs_float() -> None:
    """paren=int → без .0; paren=float дробный → через fmt_avg (один знак)."""
    html_int = _render_hero_pill(actual=100, actual_paren=4, cap=200, cap_paren=4, title='t', level=pill_level(100, 200))
    assert 'hero-pill__paren">(4)<' in html_int
    html_float = _render_hero_pill(actual=100, actual_paren=3.7, cap=200, cap_paren=4.7, title='t', level=pill_level(100, 200))
    assert 'hero-pill__paren">(3.7)<' in html_float
    assert 'hero-pill__paren">(4.7)<' in html_float

def test_render_pill_title_attr_present() -> None:
    """Tooltip из title попадает в атрибут title=. валидный HTML."""
    html = _render_hero_pill(actual=100, actual_paren=None, cap=200, cap_paren=None, title='Текущая / средняя', level=pill_level(100, 200))
    assert 'title="Текущая / средняя"' in html

def test_render_pill_default_sep_is_slash() -> None:
    """По умолчанию sep='/' — backward-compat для day-pill'а (6.27M / 10.75M)."""
    html = _render_hero_pill(actual=100, actual_paren=None, cap=200, cap_paren=None, title='t', level=pill_level(100, 200))
    assert 'hero-pill__sep">/</span>' in html
    assert '•' not in html

def test_render_pill_custom_sep() -> None:
    """Параметр sep пробрасывается в markup — нужно для combined-pill'а ('•')."""
    html = _render_hero_pill(actual=100, actual_paren=None, cap=200, cap_paren=None, title='t', sep='•', level=pill_level(100, 200))
    assert 'hero-pill__sep">•</span>' in html
    assert 'hero-pill__sep">/</span>' not in html

def test_inner_paren_inline_nests_inside_value_span() -> None:
    """paren_inline=True → скобочный суффикс ВНУТРИ value-span'а, без flex-gap.

    Проверяем, что markup для actual выглядит как
    `<span class="hero-pill__actual …">273.8K<span class="hero-pill__paren">(5)</span></span>`,
    а НЕ как два отдельных flex-child'а (тогда был бы `>273.8K</span><span class="hero-pill__paren">`).
    """
    inner = _build_hero_pill_inner(actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, sep='•', paren_inline=True, level=pill_level(273800, 877500))
    assert '<span class="hero-pill__actual hero-pill__actual--ok">273.8K<span class="hero-pill__paren">(5)</span></span>' in inner
    assert '<span class="hero-pill__cap">877.5K<span class="hero-pill__paren">(5.7)</span></span>' in inner
    assert 'hero-pill__sep">•<' in inner

def test_inner_paren_default_is_separate_child() -> None:
    """paren_inline=False (default) → paren отдельный flex-child.

    Проверяем, что скобка идёт ПОСЛЕ закрытия value-span'а, не внутри.
    Используется для standalone day-pill'а, который рендерится через
    `_render_hero_pill` (paren_inline по умолчанию False).
    """
    inner = _build_hero_pill_inner(actual=130121, actual_paren=4, cap=306239, cap_paren=4, level=pill_level(130121, 306239))
    assert '<span class="hero-pill__actual hero-pill__actual--ok">130.1K</span><span class="hero-pill__paren">(4)</span>' in inner
    assert '<span class="hero-pill__cap">306.2K</span><span class="hero-pill__paren">(4)</span>' in inner

def test_combined_pill_has_combined_class() -> None:
    """Outer span — `hero-pill hero-pill--combined`."""
    html = _render_combined_session_pill(title='agent-tokens-dashboard', duration_ms=28 * 60 * 1000, path='C:/Projects/Python/0803_agent-tokens-dashboard', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    assert 'class="hero-pill hero-pill--combined"' in html

def test_combined_pill_contains_project_duration_ratio_in_order() -> None:
    """Структура: project → duration → ratio. Проверяем позиции в markup."""
    html = _render_combined_session_pill(title='agent-tokens-dashboard', duration_ms=28 * 60 * 1000, path='C:/x', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    pos_project = html.index('agent-tokens-dashboard')
    pos_duration = html.index('28 min')
    pos_actual = html.index('273.8K')
    pos_cap = html.index('877.5K')
    assert pos_project < pos_duration < pos_actual < pos_cap, f'expected project<duration<actual<cap, got positions pos_project={pos_project!r}, pos_duration={pos_duration!r}, pos_actual={pos_actual!r}, pos_cap={pos_cap!r}'

def test_combined_pill_session_title_prepends_with_sep_dot() -> None:
    """session_record_title → ДО project, через разделитель • (свой класс sep-dot).

    Семантически session title (имя ветки) и project (папка) — разные сущности,
    2026-08-05: pill показывает оба через •. КРИТИЧНО: session идёт ПЕРЕД
    project (пользователь хочет видеть «что делаю» слева от «где»).
    """
    html = _render_combined_session_pill(title='agent-tokens-dashboard', session_record_title='TB07 Idempotency Photos', duration_ms=28 * 60 * 1000, path='C:/x', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    pos_session = html.index('TB07 Idempotency Photos')
    pos_project = html.index('agent-tokens-dashboard')
    assert pos_session < pos_project, f'expected session<project, got positions pos_session={pos_session!r}, pos_project={pos_project!r}'
    assert '<span class="hero-pill__session">TB07 Idempotency Photos</span>' in html
    assert '<span class="hero-pill__sep-dot">•</span>' in html
    assert '<span class="hero-pill__project">agent-tokens-dashboard</span>' in html

def test_combined_pill_omits_session_block_when_none() -> None:
    """session_record_title=None (default) → session+sep-dot блок НЕ рендерится.

    Backward-compat: старые runtime не пишут title, тесты без record_json.title —
    pill должен выглядеть как до 2026-08-05 (только project | duration | ratio),
    без пустого «• project» в начале.
    """
    html = _render_combined_session_pill(title='agent-tokens-dashboard', duration_ms=28 * 60 * 1000, path='C:/x', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    assert 'hero-pill__session' not in html
    assert 'hero-pill__sep-dot' not in html
    assert '<span class="hero-pill__project">agent-tokens-dashboard</span>' in html

def test_combined_pill_session_title_escapes_html() -> None:
    """XSS: session title экранируется, как и project (одна схема escape'а).

    Защита от injection в <span class="hero-pill__session">…</span> — пользователь
    может ввести title с <script>/&/"/кавычками, runtime не санитизирует.
    """
    html = _render_combined_session_pill(title='agent-tokens-dashboard', session_record_title='<script>alert(1)</script>', duration_ms=60000, path='C:/x', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert '<script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '&amp;lt;script&amp;gt;' not in html

def test_combined_pill_full_order_with_session() -> None:
    """Полный порядок с session title: session < project < duration < actual < cap."""
    html = _render_combined_session_pill(title='agent-tokens-dashboard', session_record_title='Refactor hero pill', duration_ms=28 * 60 * 1000, path='C:/x', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    pos_session = html.index('Refactor hero pill')
    pos_project = html.index('agent-tokens-dashboard')
    pos_duration = html.index('28 min')
    pos_actual = html.index('273.8K')
    pos_cap = html.index('877.5K')
    assert pos_session < pos_project < pos_duration < pos_actual < pos_cap, f'expected session<project<duration<actual<cap, got pos_session={pos_session!r}, pos_project={pos_project!r}, pos_duration={pos_duration!r}, pos_actual={pos_actual!r}, pos_cap={pos_cap!r}'

def test_combined_pill_uses_bullet_not_slash() -> None:
    """Ratio использует bullet '•' вместо '/'. TL-фидбэк 2026-08-05."""
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='p', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    ratio_start = html.index('hero-pill__ratio')
    assert 'hero-pill__sep">•</span>' in html[ratio_start:]

def test_combined_pill_paren_is_inline() -> None:
    """paren_inline=True → скобки внутри value-span'ов (плотный вид `273.8K(5)`).

    Это КЛЮЧЕВОЕ отличие от standalone session-pill'а: flex-gap родителя не
    разделяет число и скобки, иначе визуально `273.8K (5)` (с пробелом).
    """
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='p', actual=273800, actual_paren=5, cap=877500, cap_paren=5.7, level=pill_level(273800, 877500))
    assert '<span class="hero-pill__actual hero-pill__actual--ok">273.8K<span class="hero-pill__paren">(5)</span></span>' in html
    assert '<span class="hero-pill__cap">877.5K<span class="hero-pill__paren">(5.7)</span></span>' in html

def test_combined_pill_uses_ratio_wrapper() -> None:
    """Ratio-часть обёрнута в <span class="hero-pill__ratio"> для группы с
    собственной border-left (второй `|` после duration).

    Class-name `hero-pill__ratio` встречается ровно ОДИН раз — в открывающем
    теге (закрывающий — `</span>`, без class). Это и проверяем: ratio-wrapper
    реально оборачивает, а не случайный одиночный span.
    """
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='p', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert '<span class="hero-pill__ratio">' in html
    assert html.count('hero-pill__ratio') == 1

def test_combined_pill_tooltip_combines_path_and_ratio_desc() -> None:
    """Tooltip — это path + '\\n\\n' + описание ratio (сохранено из старого session-pill'а)."""
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='C:/Projects/Python/agent-tokens-dashboard', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert 'title="C:/Projects/Python/agent-tokens-dashboard' in html
    assert 'Токены в текущей сессии (запросы) / среднее по сессиям (запросы/сессию)' in html

def test_combined_pill_escapes_html_in_path_and_title() -> None:
    """XSS-защита: title и path экранируются перед вставкой в HTML.

    Если бы не escape, &, <, >, " в path/title ломали бы атрибут title= и
    позволили бы инъекцию markup'а. ВАЖНО: escape ровно один раз, иначе
    `&` от первого escape'а станет `&amp;` от второго (`&amp;quot;` вместо
    `&quot;`).
    """
    html = _render_combined_session_pill(title='<script>alert(1)</script>', duration_ms=60000, path='C:/x"<y>&z', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert '<script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '&quot;' in html
    assert '&amp;quot;' not in html
    assert '&amp;lt;' not in html
    assert '&amp;gt;' not in html
    assert '&amp;amp;' not in html

def test_combined_pill_handles_none_path() -> None:
    """path=None → в tooltip кладётся «путь не определён» (fallback для UI)."""
    html = _render_combined_session_pill(title='x', duration_ms=60000, path=None, actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert 'путь не определён' in html

def test_combined_pill_handles_none_title_and_duration() -> None:
    """title=None → '—' в .hero-pill__project; duration_ms=None → '—' в .hero-pill__duration."""
    html = _render_combined_session_pill(title=None, duration_ms=None, path='p', actual=100, actual_paren=1, cap=200, cap_paren=2.0, level=pill_level(100, 200))
    assert '<span class="hero-pill__duration">—</span>' in html
    assert '<span class="hero-pill__project">—</span>' in html

def test_combined_pill_handles_none_actual_via_dash() -> None:
    """actual=None → fmt_tokens(None) = '—' в числителе, level='none' (без цвета)."""
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='p', actual=None, actual_paren=None, cap=200, cap_paren=2.0, level=pill_level(None, 200))
    assert '<span class="hero-pill__actual">—<' in html
    assert 'hero-pill__actual--' not in html

def test_combined_pill_color_class_inherited_from_pill_level() -> None:
    """Числитель подсвечивается по pill_level: actual≤80% cap → ok (зелёный)."""
    html = _render_combined_session_pill(title='x', duration_ms=60000, path='p', actual=80000, actual_paren=4, cap=200000, cap_paren=4.0, level=pill_level(80000, 200000))
    assert 'hero-pill__actual--ok' in html

def main() -> int:
    tests = [test_pill_level_ok_below_80pct, test_pill_level_ok_at_80pct_boundary, test_pill_level_warn_above_80pct, test_pill_level_warn_at_100pct_boundary, test_pill_level_over_above_100pct, test_pill_level_none_when_actual_zero, test_pill_level_none_when_cap_zero, test_pill_level_none_when_cap_negative, test_pill_level_none_when_actual_none, test_pill_level_none_when_cap_none, test_render_pill_ok_has_green_class, test_render_pill_warn_has_orange_class, test_render_pill_over_has_red_class, test_render_pill_none_actual_no_color_class, test_render_pill_none_cap_renders_dash, test_render_pill_no_paren_when_value_none, test_render_pill_paren_int_vs_float, test_render_pill_title_attr_present, test_render_pill_default_sep_is_slash, test_render_pill_custom_sep, test_inner_paren_inline_nests_inside_value_span, test_inner_paren_default_is_separate_child, test_combined_pill_has_combined_class, test_combined_pill_contains_project_duration_ratio_in_order, test_combined_pill_session_title_prepends_with_sep_dot, test_combined_pill_omits_session_block_when_none, test_combined_pill_session_title_escapes_html, test_combined_pill_full_order_with_session, test_combined_pill_uses_bullet_not_slash, test_combined_pill_paren_is_inline, test_combined_pill_uses_ratio_wrapper, test_combined_pill_tooltip_combines_path_and_ratio_desc, test_combined_pill_escapes_html_in_path_and_title, test_combined_pill_handles_none_path, test_combined_pill_handles_none_title_and_duration, test_combined_pill_handles_none_actual_via_dash, test_combined_pill_color_class_inherited_from_pill_level]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failed += 1
        except Exception as e:
            print(f'  ERROR {t.__name__}: {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed}/{passed + failed} tests passed')
    return 0 if failed == 0 else 1
if __name__ == '__main__':
    sys.exit(main())