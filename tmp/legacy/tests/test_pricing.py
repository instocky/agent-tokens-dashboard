"""Tests for config.py pricing helpers + их использование в weekly render.

Покрывает:
  - compute_cost: корректная формула (input/1M * in + output/1M * out)
  - compute_cost: ValueError на неизвестной модели
  - fmt_money: формат '$X.XX' с thousands separator
  - weekly render: title недели содержит cost, week-total HTML содержит
    tokens + cost + разделитель (.week-total__sep)

Запуск: `python tests/test_pricing.py`.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    Week,
    _render_weekly_grid,
)
from config import (  # noqa: E402
    DEFAULT_MODEL,
    MODEL_PRICING,
    compute_cost,
    fmt_money,
)


# ---- compute_cost ---------------------------------------------------------

def test_compute_cost_basic() -> None:
    """1M input + 1M output по DEFAULT_MODEL → 0.23 + 0.96 = $1.19."""
    cost = compute_cost(1_000_000, 1_000_000)
    assert abs(cost - 1.19) < 1e-9, f"got {cost!r}"


def test_compute_cost_only_input() -> None:
    """Только input, output=0 → (in/1M) * price_in."""
    cost = compute_cost(2_000_000, 0)
    assert abs(cost - 0.46) < 1e-9, f"got {cost!r}"


def test_compute_cost_only_output() -> None:
    """Только output, input=0 → (out/1M) * price_out."""
    cost = compute_cost(0, 500_000)
    assert abs(cost - 0.48) < 1e-9, f"got {cost!r}"


def test_compute_cost_zero() -> None:
    """0 input + 0 output → 0.0."""
    assert compute_cost(0, 0) == 0.0


def test_compute_cost_unknown_model_raises() -> None:
    """Неизвестная модель → ValueError (явный fail-fast, не молчаливый $0)."""
    try:
        compute_cost(1_000_000, 0, model="gpt-99-ultra")
    except ValueError as exc:
        assert "gpt-99-ultra" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_compute_cost_uses_default_when_model_omitted() -> None:
    """model= берётся из DEFAULT_MODEL, если не передан."""
    cost_explicit = compute_cost(1_000_000, 0, model=DEFAULT_MODEL)
    cost_implicit = compute_cost(1_000_000, 0)
    assert cost_explicit == cost_implicit


# ---- fmt_money ------------------------------------------------------------

def test_fmt_money_basic() -> None:
    assert fmt_money(5.4) == "$5.40"


def test_fmt_money_zero() -> None:
    assert fmt_money(0.0) == "$0.00"


def test_fmt_money_with_thousands() -> None:
    """1234.5 → '$1,234.50' (thousands separator)."""
    assert fmt_money(1234.5) == "$1,234.50"


def test_fmt_money_sub_cent_rounds() -> None:
    """Half-up rounding по банковской конвенции Python format."""
    # 0.005 в Python 3 rounds to even (banker's); 0.001 → 0.00.
    assert fmt_money(0.001) == "$0.00"


# ---- integration: weekly render использует цену --------------------------

def _make_week(label: str, monday: date, in_total: int, out_total: int) -> Week:
    """Week с одним днём данных для проверки рендера."""
    return Week(
        label=label,
        monday=monday,
        days=[in_total + out_total, None, None, None, None, None, None],
        days_split=[(in_total, out_total), None, None, None, None, None, None],
        is_current=True,
    )


def test_weekly_grid_title_contains_cost() -> None:
    """Title недельной шапки содержит fmt_money(compute_cost(week_in, week_out))."""
    week = _make_week("W-32", date(2026, 8, 3), 1_000_000, 1_000_000)
    # week_in = 1M, week_out = 1M → cost = 1.19
    html = _render_weekly_grid([week], "linear", 1_000_000 * 2)
    assert "$1.19" in html, f"expected $1.19 in weekly title, got:\n{html[:500]}"


def test_weekly_grid_total_html_has_fraction() -> None:
    """week-total HTML содержит три элемента: tokens span, sep, cost span."""
    week = _make_week("W-32", date(2026, 8, 3), 2_000_000, 1_000_000)
    # total = 3M; cost = 0.46 + 0.96 = 1.42
    html = _render_weekly_grid([week], "linear", 3_000_000)
    assert 'class="week-total__tokens"' in html
    assert 'class="week-total__sep"' in html
    assert 'class="week-total__cost"' in html
    assert "$1.42" in html
    assert "3.00M" in html


def test_weekly_grid_bar_title_has_cost() -> None:
    """Tooltip дневного бара содержит цену."""
    week = _make_week("W-32", date(2026, 8, 3), 1_000_000, 500_000)
    # in=1M, out=500K → cost = 0.23 + 0.48 = 0.71
    html = _render_weekly_grid([week], "linear", 1_500_000)
    assert "$0.71" in html, f"expected $0.71 in bar tooltip:\n{html[:500]}"


def test_default_model_in_pricing() -> None:
    """DEFAULT_MODEL есть в MODEL_PRICING (sanity check, иначе compute_cost
    падает на каждом рендере)."""
    assert DEFAULT_MODEL in MODEL_PRICING, (
        f"DEFAULT_MODEL={DEFAULT_MODEL!r} не найден в MODEL_PRICING"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
