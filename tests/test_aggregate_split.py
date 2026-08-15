"""Tests for aggregate_by_hour_split (SQL → split dict).

Покрывает:
  - SQL возвращает (input_tokens, output_tokens) раздельно
  - aggregate_by_hour (legacy) суммирует их обратно в int
  - группировка по (date, hour) — несколько строк в одной группе
  - since_msk_date фильтр работает (старые строки исключены)
  - пустая таблица → пустой dict

Запуск: `python tests/test_aggregate_split.py`.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import (  # noqa: E402
    MSK,
    aggregate_by_hour,
    aggregate_by_hour_split,
)


def _make_db() -> sqlite3.Connection:
    """In-memory SQLite с таблицей local_runtime_token_usage."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE local_runtime_token_usage (
            ts            INTEGER NOT NULL,   -- ms epoch (UTC)
            session_id    TEXT,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        );
    """)
    return con


def _insert_msk(con: sqlite3.Connection, msk_date: date, hour: int,
                in_tok: int, out_tok: int, session_id: str = "s1") -> None:
    """Вставить строку с ts, приведённым к MSK (date, hour)."""
    # UTC ms для msk_date+03:00 hour:00:00
    msk_dt = datetime.combine(msk_date, datetime.min.time(), tzinfo=MSK).replace(hour=hour)
    utc_dt = msk_dt.astimezone(__import__("datetime").timezone.utc)
    ts_ms = int(utc_dt.timestamp() * 1000)
    con.execute(
        "INSERT INTO local_runtime_token_usage (ts, session_id, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (ts_ms, session_id, in_tok, out_tok),
    )


def test_aggregate_split_basic() -> None:
    """Одна строка → (input, output) в правильной (date, hour) ячейке."""
    con = _make_db()
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=1_000, out_tok=4_000)
    out = aggregate_by_hour_split(con, date(2026, 8, 4))
    assert out == {(date(2026, 8, 4), 10): (1_000, 4_000)}, f"got {out}"


def test_aggregate_split_groups_multiple_rows() -> None:
    """Несколько строк в одной (date, hour) группе → SUM input, SUM output."""
    con = _make_db()
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=100, out_tok=200, session_id="s1")
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=300, out_tok=400, session_id="s2")
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=600, out_tok=800, session_id="s3")
    out = aggregate_by_hour_split(con, date(2026, 8, 4))
    # 100+300+600=1000 in, 200+400+800=1400 out
    assert out[(date(2026, 8, 4), 10)] == (1_000, 1_400)


def test_aggregate_split_filters_by_since() -> None:
    """since_msk_date отсекает более старые строки."""
    con = _make_db()
    _insert_msk(con, date(2026, 7, 30), 10, in_tok=999, out_tok=999)  # до since
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=100, out_tok=200)   # ≥ since
    out = aggregate_by_hour_split(con, date(2026, 8, 4))
    assert (date(2026, 7, 30), 10) not in out, "old row leaked into output"
    assert out[(date(2026, 8, 4), 10)] == (100, 200)


def test_aggregate_split_empty_table() -> None:
    """Пустая таблица → пустой dict."""
    con = _make_db()
    out = aggregate_by_hour_split(con, date(2026, 8, 4))
    assert out == {}


def test_aggregate_hour_is_sum_of_split() -> None:
    """aggregate_by_hour (legacy) = sum от aggregate_by_hour_split (контракт)."""
    con = _make_db()
    _insert_msk(con, date(2026, 8, 4), 10, in_tok=100, out_tok=200)
    _insert_msk(con, date(2026, 8, 4), 11, in_tok=300, out_tok=0)
    _insert_msk(con, date(2026, 8, 5), 10, in_tok=0, out_tok=500)

    split = aggregate_by_hour_split(con, date(2026, 8, 4))
    summed = aggregate_by_hour(con, date(2026, 8, 4))
    assert len(split) == len(summed)
    for k, (in_v, out_v) in split.items():
        assert summed[k] == in_v + out_v, f"key={k}: split={in_v}+{out_v}, sum={summed[k]}"


def test_aggregate_split_handles_utc_vs_msk_correctly() -> None:
    """Преобразование ms→MSK: строка с UTC ts должна попасть в правильный MSK hour.

    Edge case: UTC=22:00, MSK=01:00 следующего дня (для летнего MSK +0,
    для зимнего +3 — мы фиксируем UTC=22:00, MSK=01:00 (+3)).
    """
    con = _make_db()
    # MSK Aug 5 01:00 = UTC Aug 4 22:00.
    utc_dt = datetime(2026, 8, 4, 22, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    ts_ms = int(utc_dt.timestamp() * 1000)
    con.execute(
        "INSERT INTO local_runtime_token_usage (ts, session_id, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (ts_ms, "s1", 50, 150),
    )
    out = aggregate_by_hour_split(con, date(2026, 8, 4))
    # Должно попасть в (2026-08-05, 01:00) MSK, не в (2026-08-04, 22:00).
    assert (date(2026, 8, 5), 1) in out, f"MSK shift broken: keys={list(out.keys())}"
    assert out[(date(2026, 8, 5), 1)] == (50, 150)


def main() -> int:
    """Прогон всех test_* функций в этом модуле. Возвращает exit code."""
    import traceback
    tests = [
        test_aggregate_split_basic,
        test_aggregate_split_groups_multiple_rows,
        test_aggregate_split_filters_by_since,
        test_aggregate_split_empty_table,
        test_aggregate_hour_is_sum_of_split,
        test_aggregate_split_handles_utc_vs_msk_correctly,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"  ERROR {t.__name__}:")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
