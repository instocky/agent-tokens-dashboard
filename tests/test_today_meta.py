"""Tests for `compute_today_meta` and `fmt_avg` — sub-line карточки «Сегодня».

Карточка «Сегодня» теперь показывает три meta-метрики:
  - avg (requests per session)
  - sessions (DISTINCT session_id)
  - user requests (role='user')

Покрывает:
  - `fmt_avg`: целые без '.0', дробные с одним знаком, edge cases (0.0, 0.5).
  - `compute_today_meta`:
      - empty day: (0, 0, 0.0)
      - только user-сообщения в одной сессии: (1, N, N.0)
      - несколько сессий с user + non-user: корректные sessions/user_count/avg
      - граница 00:00 MSK: события до полуночи НЕ считаются
      - role IS NULL: не считаются как user
      - та же сессия с user + assistant: 1 session, 1 user request

Запуск: `python tests/test_today_meta.py`.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_dashboard import MSK, compute_today_meta, fmt_avg  # noqa: E402


# ---- fixtures --------------------------------------------------------------


_FIXED_NOW = datetime(2026, 8, 4, 9, 30, 0, tzinfo=MSK)  # 09:30 MSK
_TODAY_MIDNIGHT_MS = int(
    datetime(2026, 8, 4, tzinfo=MSK).timestamp() * 1000
)


def _make_con() -> sqlite3.Connection:
    """In-memory SQLite с минимальной схемой local_runtime_message_rows.

    Схема — точная копия из production (только нужные колонки). Index
    по created_at_ms НЕ создаём — тестируем именно full-scan поведение.
    """
    con = sqlite3.connect(":memory:")
    con.execute(
        """
        CREATE TABLE local_runtime_message_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            role TEXT,
            turn_id TEXT,
            created_at_ms INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            UNIQUE(session_id, msg_id)
        )
        """
    )
    return con


def _insert(
    con: sqlite3.Connection,
    *,
    session_id: str,
    role: str | None,
    created_at_ms: int,
    msg_id: str | None = None,
) -> None:
    """Хелпер для вставки одной строки. msg_id по умолчанию уникальный."""
    if msg_id is None:
        msg_id = f"{session_id}:{created_at_ms}"
    con.execute(
        "INSERT INTO local_runtime_message_rows "
        "(session_id, msg_id, role, turn_id, created_at_ms, data_json) "
        "VALUES (?, ?, ?, NULL, ?, '{}')",
        (session_id, msg_id, role, created_at_ms),
    )


# ---- fmt_avg ---------------------------------------------------------------


def test_fmt_avg_integer_without_dot_zero() -> None:
    """Целые значения: 2.0 → '2', 3.0 → '3', 0.0 → '0'.

    Обсуждение 2026-08-04: '.0' у целых — визуальный шум.
    """
    assert fmt_avg(2.0) == "2"
    assert fmt_avg(3.0) == "3"
    assert fmt_avg(0.0) == "0"


def test_fmt_avg_fractional_one_decimal() -> None:
    """Дробные: всегда один знак после запятой."""
    assert fmt_avg(3.5) == "3.5"
    assert fmt_avg(2.7) == "2.7"
    assert fmt_avg(0.5) == "0.5"
    assert fmt_avg(0.04) == "0.0"  # округление до 1 знака
    assert fmt_avg(0.05) == "0.1"  # banker's? в Python 3 это 0.1


def test_fmt_avg_large_values() -> None:
    """Большие значения (avg > 10): один знак после запятой у дробных, без .0 у целых."""
    assert fmt_avg(15.0) == "15"
    assert fmt_avg(15.5) == "15.5"
    assert fmt_avg(123.4) == "123.4"


# ---- compute_today_meta: empty / edge cases --------------------------------


def test_today_meta_empty_day() -> None:
    """Нет строк в окне — (0, 0, 0.0)."""
    con = _make_con()
    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 0
    assert user_msgs == 0
    assert avg == 0.0


def test_today_meta_only_non_user_roles() -> None:
    """Только assistant/tool/None: sessions>0, user_messages=0, avg=0.0.

    sessions считает любую активность (включая non-user), user_messages=0 →
    avg=0.0 (не деление на ноль).
    """
    con = _make_con()
    # 2 сессии, в каждой только assistant.
    _insert(con, session_id="s1", role="assistant", created_at_ms=_TODAY_MIDNIGHT_MS + 1000)
    _insert(con, session_id="s1", role="tool",     created_at_ms=_TODAY_MIDNIGHT_MS + 2000)
    _insert(con, session_id="s2", role="assistant", created_at_ms=_TODAY_MIDNIGHT_MS + 3000)
    _insert(con, session_id="s2", role=None,        created_at_ms=_TODAY_MIDNIGHT_MS + 4000)
    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 2
    assert user_msgs == 0
    assert avg == 0.0


# ---- compute_today_meta: single session -----------------------------------


def test_today_meta_single_session_only_user() -> None:
    """Одна сессия, 4 user-сообщения, остальные non-user → (1, 4, 4.0)."""
    con = _make_con()
    base = _TODAY_MIDNIGHT_MS
    for i, role in enumerate(["user", "assistant", "user", "tool", "user", "user", "assistant"]):
        _insert(con, session_id="s1", role=role, created_at_ms=base + 1000 * i)
    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 1
    assert user_msgs == 4
    assert avg == 4.0


# ---- compute_today_meta: multiple sessions --------------------------------


def test_today_meta_multiple_sessions_realistic() -> None:
    """Реалистичный кейс (2026-08-04 09:30 MSK, наши 2 сессии):
       - session A: 3 user, 15 assistant
       - session B: 4 user, 163 assistant, 12 None
       Итого: sessions=2, user=7, avg=3.5.
    """
    con = _make_con()
    base = _TODAY_MIDNIGHT_MS

    # Session A (наша текущая): 3 user + 15 assistant
    for i in range(3):
        _insert(con, session_id="mvs_aaa", role="user", created_at_ms=base + 1000 * i)
    for i in range(15):
        _insert(con, session_id="mvs_aaa", role="assistant",
                created_at_ms=base + 10_000 + 1000 * i)

    # Session B (чужая, автономная): 4 user + 163 assistant + 12 None
    for i in range(4):
        _insert(con, session_id="mvs_bbb", role="user", created_at_ms=base + 5000 * i)
    for i in range(163):
        _insert(con, session_id="mvs_bbb", role="assistant",
                created_at_ms=base + 50_000 + 1000 * i)
    for i in range(12):
        _insert(con, session_id="mvs_bbb", role=None,
                created_at_ms=base + 1_000_000 + 1000 * i)

    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 2
    assert user_msgs == 7
    assert avg == 3.5


def test_today_meta_distinct_sessions() -> None:
    """Один и тот же session_id в 10 строках = 1 сессия, а не 10.

    Защита от регрессии: если бы мы считали без DISTINCT, счётчик бы
    завысился. Здесь avg тоже подскажет: при 1 session и 5 user → avg=5.0,
    а не 0.5.
    """
    con = _make_con()
    base = _TODAY_MIDNIGHT_MS
    for i in range(5):
        _insert(con, session_id="s1", role="user", created_at_ms=base + 1000 * i)
    for i in range(5):
        _insert(con, session_id="s1", role="assistant", created_at_ms=base + 10_000 + 1000 * i)
    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 1
    assert user_msgs == 5
    assert avg == 5.0


# ---- compute_today_meta: time boundary -------------------------------------


def test_today_meta_boundary_excludes_yesterday() -> None:
    """События ДО 00:00 MSK (вчера) НЕ попадают в окно.

    Берём 1 user-сообщение за секунду до полуночи, 1 user-сообщение
    через секунду после. В окне должна быть только вторая.
    """
    con = _make_con()
    # Вчера, 23:59:59.999 — последний момент вчерашнего дня.
    yesterday_ms = _TODAY_MIDNIGHT_MS - 1
    _insert(con, session_id="s1", role="user", created_at_ms=yesterday_ms)
    # Сегодня, 00:00:00.000 — ровно полночь.
    _insert(con, session_id="s1", role="user", created_at_ms=_TODAY_MIDNIGHT_MS)
    # Сегодня, позже.
    _insert(con, session_id="s2", role="user", created_at_ms=_TODAY_MIDNIGHT_MS + 60_000)

    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 2
    assert user_msgs == 2  # 1 у s1 + 1 у s2
    assert avg == 1.0


def test_today_meta_window_end_is_open() -> None:
    """Окно — полуоткрытое [00:00, now_msk). События, попавшие ровно на
    now_msk (граница), НЕ включаются — но в нашей реализации мы фильтруем
    только `created_at_ms >= midnight`, так что события позже now_msk всё
    равно войдут. Это сознательно: контракт fix-в-будущем, сейчас важнее
    простота, а расхождение с now_msk на сотни мс — нерелевантно.
    """
    con = _make_con()
    # Событие далеко в будущем (после now_msk) — должно попасть.
    # Это документированное поведение; в проде такого не бывает (БД
    # не содержит записей из будущего), но контракт это фиксирует.
    future_ms = int(_FIXED_NOW.timestamp() * 1000) + 86_400_000  # +24h
    _insert(con, session_id="s1", role="user", created_at_ms=future_ms)
    sessions, user_msgs, avg = compute_today_meta(con, _FIXED_NOW)
    assert sessions == 1
    assert user_msgs == 1


# ---- main ------------------------------------------------------------------


def main() -> int:
    tests = [
        # fmt_avg
        test_fmt_avg_integer_without_dot_zero,
        test_fmt_avg_fractional_one_decimal,
        test_fmt_avg_large_values,
        # compute_today_meta
        test_today_meta_empty_day,
        test_today_meta_only_non_user_roles,
        test_today_meta_single_session_only_user,
        test_today_meta_multiple_sessions_realistic,
        test_today_meta_distinct_sessions,
        test_today_meta_boundary_excludes_yesterday,
        test_today_meta_window_end_is_open,
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
