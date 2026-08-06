"""Smoke-тесты для render_analytics.render(snapshot).

Покрывают HTML-структуру calendar heatmap (4 недели × 7 дней) на фиктивном
snapshot'е. Ловят чисто визуальные регрессии: сломанная сетка, потерянные
классы, отсутствующая мета-строка, плохая экранизация текста.

Запуск: `python tests/test_render_analytics.py`.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import (  # noqa: E402
    MSK,
    DailyBar,
)


def _make_daily_bars(today: date) -> list[DailyBar]:
    """28-дневный сценарий: today=Ср W-0, есть данные в разных неделях,
    есть empty, есть future. Используется во всех тестах."""
    monday_w3 = today - timedelta(days=today.weekday() + 3 * 7)
    bars: list[DailyBar] = []
    # Распределение значений по 28 дням — половина дней пустая.
    values_per_day: dict[date, int | None] = {}
    for i in range(28):
        d = monday_w3 + timedelta(days=i)
        if d == today:
            values_per_day[d] = 5_000_000  # current
        elif d > today:
            values_per_day[d] = None  # future
        elif i % 4 == 0:
            values_per_day[d] = None  # empty
        else:
            values_per_day[d] = (i + 1) * 1_000_000  # active
    for d, v in values_per_day.items():
        if d > today:
            state = "future"
            intensity = None
        elif d == today:
            state = "current"
            intensity = "L3"
        elif v is None:
            state = "empty"
            intensity = None
        else:
            state = "active"
            intensity = "L2"
        bars.append(
            DailyBar(
                date=d,
                value=v,
                state=state,
                intensity=intensity,
                weekday=d.weekday(),
                iso_week=d.isocalendar()[1],
                is_current_week=(d >= today - timedelta(days=today.weekday())),
            )
        )
    return bars


def _snapshot(today: date, **overrides) -> dict:
    """Базовый snapshot с фиктивной daily-секцией."""
    base = {
        "now_msk": datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=MSK),
        "daily": {
            "since": today - timedelta(days=today.weekday() + 3 * 7),
            "weeks": _make_daily_bars(today),
            "current_weekday": today.weekday(),
            "weekly_cap": 60_000_000,
            "burn_today": "ok",
            "burn_7d_avg": 2_500_000,
        },
    }
    for k, v in overrides.items():
        base[k] = v
    return base


# ---- структурные проверки -------------------------------------------------


def test_render_returns_self_contained_html() -> None:
    """render() возвращает валидный HTML5 self-contained документ."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    assert html.startswith("<!DOCTYPE html>"), "нет DOCTYPE"
    assert "<html lang=\"ru\">" in html
    assert "</html>" in html
    assert "<meta charset=\"UTF-8\" />" in html
    assert 'http-equiv="refresh" content="60"' in html
    # self-contained: inline CSS, без <script> (статичный heatmap, ADR-002 §4).
    assert "<style>" in html
    assert "<script" not in html


def test_render_grid_has_4_columns_7_rows() -> None:
    """4 колонки (W-3..W-0), в каждой 7 ячеек (Пн..Вс)."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    # Колонки: ровно 3 без current-week + 1 с current-week.
    # Паттерн с закрывающей кавычкой сразу после `daily-col` ловит только
    # именно колонку, не `.daily-col-head` (там `class="daily-col-head"`).
    plain_cols = html.count('<div class="daily-col">')
    cw_cols = html.count('<div class="daily-col current-week">')
    assert plain_cols + cw_cols == 4, (
        f"ожидалось 4 колонки, получили plain={plain_cols} + cw={cw_cols}"
    )
    assert cw_cols == 1, f"current-week должна быть ровно одна, получили {cw_cols}"
    # 28 ячеек: 4 недели × 7 дней.
    assert html.count('<div class="daily-cell') == 28, (
        f"ожидалось 28 ячеек, получили {html.count('<div class=\"daily-cell')}"
    )


def test_render_legend_present() -> None:
    """В meta-строке есть легенда L1..L4."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    assert "daily-legend" in html
    for lvl in ("L1", "L2", "L3", "L4"):
        assert f"intensity-{lvl}" in html, f"нет intensity-{lvl}"


def test_render_burn_line_with_level() -> None:
    """Meta-строка содержит burn-уровень и 7d avg."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    assert "burn сегодня" in html
    assert "<strong>ok</strong>" in html
    assert "2.50M" in html  # burn_7d_avg formatted


def test_render_burn_none_when_no_data() -> None:
    """burn_today='none' → в meta '—', без <strong>ok/warn/over.</strong>."""
    from render_analytics import render
    snap = _snapshot(date(2026, 8, 5))
    snap["daily"]["burn_today"] = "none"
    snap["daily"]["burn_7d_avg"] = None
    html = render(snap)
    assert "burn сегодня: —" in html
    assert "<strong>ok</strong>" not in html
    assert "<strong>warn</strong>" not in html
    assert "<strong>over</strong>" not in html


def test_render_cell_classes_for_all_states() -> None:
    """Каждое из 4 состояний имеет свой CSS-класс."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    assert "state-active" in html
    assert "state-current" in html
    assert "state-empty" in html
    assert "state-future" in html


def test_render_current_cell_outline_via_current_week() -> None:
    """Текущая неделя (W-0) — все 7 ячеек имеют .current-week."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    # 7 ячеек W-0 + 1 сам col-head → ищем 7 вхождений "daily-cell ... current-week"
    import re
    cells_with_cw = re.findall(
        r'<div class="daily-cell[^"]*current-week[^"]*"', html
    )
    assert len(cells_with_cw) == 7, (
        f"ожидалось 7 ячеек W-0 с .current-week, получили {len(cells_with_cw)}"
    )


def test_render_titles_contain_iso_date_and_value() -> None:
    """Tooltip каждой ячейки содержит DD.MM.YYYY, weekday и value."""
    from render_analytics import render
    html = render(_snapshot(date(2026, 8, 5)))
    # 28 title= атрибутов (по одному на ячейку)
    assert html.count('title="') >= 28
    # current day попадает в tooltip
    assert "05.08.2026" in html
    assert "5.00M" in html  # fmt_tokens(5_000_000)


def test_render_no_compute_calls() -> None:
    """render() не импортирует compute-функции и не открывает SQLite.

    Документирующее утверждение (правило 8 §2.2 ADR-001): модуль импортирует
    из analytics только константы/типы/форматтеры, и это видно статически.
    Проверяем AST, а не сырой текст: docstring и комментарии не мешают.
    """
    import ast
    from render_analytics import render
    import render_analytics
    src = Path(render_analytics.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Собираем все имена из всех `from analytics import ...` и `import X`.
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    forbidden = {
        "compute_daily_4w", "aggregate_by_hour", "open_db",
        "sqlite3", "pill_level", "project_title_from_path",
        "compute_weekly", "compute_today_24h", "_intensity_level",
    }
    leaked = imported & forbidden
    assert not leaked, f"render_analytics не должен импортировать: {leaked}"
    # render() не принимает ничего кроме snapshot.
    import inspect
    sig = inspect.signature(render)
    params = list(sig.parameters.keys())
    assert params == ["snapshot"], f"render() должна принимать ровно snapshot, сигнатура={params}"


# ---- main ------------------------------------------------------------------


def main() -> int:
    tests = [
        test_render_returns_self_contained_html,
        test_render_grid_has_4_columns_7_rows,
        test_render_legend_present,
        test_render_burn_line_with_level,
        test_render_burn_none_when_no_data,
        test_render_cell_classes_for_all_states,
        test_render_current_cell_outline_via_current_week,
        test_render_titles_contain_iso_date_and_value,
        test_render_no_compute_calls,
    ]
    passed = 0
    failed: list[str] = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}\n    {e}")
            failed.append(t.__name__)
    print(f"\n{passed}/{len(tests)} tests passed")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
