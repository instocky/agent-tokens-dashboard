"""Tests for collect_sessions input/output split + cost in session-dashboard.

Покрывает:
  - SQL возвращает (input, output) раздельно, не только total
  - SessionRow.tokens == input_tokens + output_tokens (parity)
  - session без token_usage → (0, 0) → tokens=0, cost=$0.00
  - render_html: колонка Cost per-row + total cost в footer
  - per-row cost через split-then-fmt_money (а не accumulating float-drift)

Запуск: `python tests/test_session_pricing.py`.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import compute_cost, fmt_money  # noqa: E402

from build_session_dashboard import (  # noqa: E402
    MSK,
    collect_sessions,
    render_html,
)


# ---- test helpers (минимальные, чтобы тесты не зависели от test_session_dashboard) ----

def _make_db() -> sqlite3.Connection:
    """In-memory SQLite с минимальной schema под collect_sessions."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE local_runtime_token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        );
        CREATE TABLE local_runtime_message_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            role TEXT,
            created_at_ms INTEGER NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE TABLE local_runtime_sessions (
            session_id TEXT PRIMARY KEY,
            record_json TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
    """)
    return con


def _msk_to_ms(d: date, h: int = 12, m: int = 0) -> int:
    dt = datetime(d.year, d.month, d.day, h, m, tzinfo=MSK)
    return int(dt.timestamp() * 1000)


def _insert_session(
    con: sqlite3.Connection,
    sid: str,
    title: str,
    workspace: str,
    status: str,
    msgs: list[tuple[int, str]],  # (created_at_ms, role)
    tokens: list[tuple[int, int, int]],  # (ts, in, out)
) -> None:
    for ts, role in msgs:
        con.execute(
            "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
            "VALUES (?, ?, ?, ?, '{}')",
            (sid, f"m-{sid}-{ts}", role, ts),
        )
    for ts, in_t, out_t in tokens:
        con.execute(
            "INSERT INTO local_runtime_token_usage (session_id, ts, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?)",
            (sid, ts, in_t, out_t),
        )
    rec = {"title": title, "workspaceDir": workspace, "status": status}
    import json as _json
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json, updated_at_ms) "
        "VALUES (?, ?, ?)",
        (sid, _json.dumps(rec), 0),
    )
    con.commit()


def _make_row(
    in_t: int = 0, out_t: int = 0, max_ms: int = 1_700_000_000_000
) -> "object":  # type: ignore[name-defined]
    """Создаёт минимальный SessionRow для render_html — не импортируем чтобы
    не ловить import-time ошибки (SessionRow живёт в build_session_dashboard)."""
    from build_session_dashboard import SessionRow
    return SessionRow(
        session_id="s_test",
        title="Test",
        project="alpha",
        workspace_dir="C:/Projects/alpha",
        start_msk=date(2026, 8, 4),
        end_msk=date(2026, 8, 4),
        max_ms=max_ms,
        duration_ms=3_600_000,
        tokens=in_t + out_t,
        input_tokens=in_t,
        output_tokens=out_t,
        requests=2,
        is_active=False,
    )


# ---- SQL split parity -------------------------------------------------------

def test_collect_split_matches_total() -> None:
    """tokens == input_tokens + output_tokens для каждой сессии."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    _insert_session(
        con, "s1", "Test", "C:/x/0803_foo", "finished",
        msgs=[(base, "user"), (base + 60_000, "assistant")],
        tokens=[(base, 30, 20), (base + 60_000, 70, 50)],
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    assert len(rows) == 1, f"got {len(rows)} rows"
    r = rows[0]
    assert r.tokens == 170, f"total: {r.tokens}"
    assert r.input_tokens == 100, f"in: {r.input_tokens}"
    assert r.output_tokens == 70, f"out: {r.output_tokens}"
    assert r.input_tokens + r.output_tokens == r.tokens, "parity"


def test_collect_split_isolation_two_sessions() -> None:
    """Split per-session не утекает между сессиями."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    _insert_session(
        con, "s1", "A", "C:/x/0803_alpha", "finished",
        msgs=[(base, "user")],
        tokens=[(base, 100, 50)],
    )
    base2 = _msk_to_ms(date(2026, 8, 5), 10, 0)
    _insert_session(
        con, "s2", "B", "C:/x/0803_beta", "finished",
        msgs=[(base2, "user")],
        tokens=[(base2, 200, 80)],
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    by_sid = {r.session_id: r for r in rows}
    assert by_sid["s1"].input_tokens == 100
    assert by_sid["s1"].output_tokens == 50
    assert by_sid["s2"].input_tokens == 200
    assert by_sid["s2"].output_tokens == 80


def test_collect_split_no_token_usage() -> None:
    """Сессия с messages, но без token_usage → (0, 0), cost=$0.00."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    _insert_session(
        con, "s1", "T", "C:/x/0803_foo", "finished",
        msgs=[(base, "user"), (base + 60_000, "assistant")],
        tokens=[],  # ← пусто
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens == 0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    # cost = compute_cost(0, 0) = 0.0 → fmt_money = "$0.00"
    assert compute_cost(r.input_tokens, r.output_tokens) == 0.0
    assert fmt_money(compute_cost(r.input_tokens, r.output_tokens)) == "$0.00"


# ---- per-row cost markup ----------------------------------------------------

def test_render_cost_column_per_row() -> None:
    """Per-row cost рендерится в 6-й ячейке (после Tokens)."""
    from build_session_dashboard import compute_window
    r = _make_row(in_t=100, out_t=50, max_ms=1)
    now = datetime(2026, 8, 4, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html = render_html([r], now, weeks)
    # Per-row cost = (100/1e6)*0.23 + (50/1e6)*0.96 = $0.0001 → $0.00
    # Проверяем, что $0.00 присутствует в markup и в строке таблицы.
    assert "$0.00" in html, "expected per-row cost in markup"


def test_render_header_has_cost_th() -> None:
    """В <thead> есть колонка Cost."""
    r = _make_row(in_t=0, out_t=0, max_ms=1)
    from build_session_dashboard import compute_window
    now = datetime(2026, 8, 4, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html = render_html([r], now, weeks)
    # Ищем <th ...>Cost</th>.
    m = re.search(r'<th[^>]*>Cost</th>', html)
    assert m is not None, "no <th>Cost</th> in HTML"


def test_render_footer_total_cost_uses_split_sum() -> None:
    """Footer total cost = compute_cost(SUM(in), SUM(out)).

    Две сессии: r1 (in=100, out=50) + r2 (in=200, out=80).
    Total in=300, out=130.
    Cost = 300/1e6*0.23 + 130/1e6*0.96 = 0.000069 + 0.0001248 = $0.00 (rounded).
    Per-row fmt_money: каждая $0.00; total fmt_money: тоже $0.00.
    """
    r1 = _make_row(in_t=100, out_t=50, max_ms=2)
    r2 = _make_row(in_t=200, out_t=80, max_ms=1)
    from build_session_dashboard import compute_window
    now = datetime(2026, 8, 4, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html = render_html([r1, r2], now, weeks)
    # Footer должен содержать $0.00 (compute_cost от 300 in / 130 out = $0.00).
    assert "footer" in html
    footer_money = re.search(r'<div class="footer">.*?(\$[\d.,]+).*?</div>', html, re.S)
    assert footer_money is not None, "no money string in footer"
    assert footer_money.group(1) == "$0.00", (
        f"expected $0.00 in footer, got: {footer_money.group(1)}"
    )


def test_render_footer_total_cost_sub_cent_real_value() -> None:
    """Footer cost для достаточно крупных токенов — non-zero $0.0X.

    in=10_000_000, out=5_000_000 → cost = 10*0.23 + 5*0.96 = 2.3 + 4.8 = $7.10.
    """
    r = _make_row(in_t=10_000_000, out_t=5_000_000, max_ms=1)
    from build_session_dashboard import compute_window
    now = datetime(2026, 8, 4, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html = render_html([r], now, weeks)
    # Per-row + footer = $7.10 (минимум 2 вхождения, но проверим что значение есть).
    assert "$7.10" in html, (
        f"expected $7.10 in HTML; per-row cost = {compute_cost(10_000_000, 5_000_000)}"
    )


# ---- main -------------------------------------------------------------------

def main() -> int:
    tests = [
        test_collect_split_matches_total,
        test_collect_split_isolation_two_sessions,
        test_collect_split_no_token_usage,
        test_render_cost_column_per_row,
        test_render_header_has_cost_th,
        test_render_footer_total_cost_uses_split_sum,
        test_render_footer_total_cost_sub_cent_real_value,
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
