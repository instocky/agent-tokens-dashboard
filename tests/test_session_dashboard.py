"""Tests for build_session_dashboard.py.

Запускается напрямую: `python tests/test_session_dashboard.py`. Без pytest —
тот же стиль, что и в tests/test_windows.py, test_weekly_cap.py и т.д.
Каждый блок печатает PASS/FAIL со сводкой; код выхода 0 если все зелёные.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_session_dashboard import (  # noqa: E402
    MSK,
    SessionRow,
    collect_sessions,
    compute_window,
    format_date_cell,
    format_duration,
    format_tokens,
    project_from_workspace,
    render_html,
)


# ---- test runner -----------------------------------------------------------

failures: list[str] = []
checks: list[tuple[str, bool, str]] = []  # (name, ok, detail)


def check(name: str, cond: bool, detail: str = "") -> None:
    checks.append((name, cond, detail))
    if not cond:
        # ASCII-only — консоль Windows по умолчанию cp1251, не пропустит ✗/✓.
        failures.append(f"  FAIL {name}: {detail}")


# ---- compute_window --------------------------------------------------------

def test_compute_window_friday_w32() -> None:
    """2026-08-07 (Fri, W-32) → 4 weeks: W-28..W-31, [Jul 6, Aug 3)."""
    today = date(2026, 8, 7)
    start_dt, end_dt, weeks = compute_window(today)
    check("friday/start_dt", start_dt.date() == date(2026, 7, 6),
          f"got {start_dt.date()}")
    check("friday/end_dt", end_dt.date() == date(2026, 8, 10),
          f"got {end_dt.date()}")
    check("friday/weeks_count", len(weeks) == 5, f"got {len(weeks)}")
    expected_labels = ["W-28", "W-29", "W-30", "W-31", "W-32"]
    actual_labels = [w.label for w in weeks]
    check("friday/week_labels", actual_labels == expected_labels,
          f"got {actual_labels}")
    # Самая ранняя — Пн Jul 6, самая поздняя — Вс Aug 9.
    check("friday/first_monday", weeks[0].monday == date(2026, 7, 6),
          f"got {weeks[0].monday}")
    check("friday/last_sunday", weeks[-1].sunday == date(2026, 8, 9),
          f"got {weeks[-1].sunday}")


def test_compute_window_monday_w32() -> None:
    """2026-08-03 (Mon, W-32 starts) — current_monday=today, конец окна
    сдвигается на +7 дней, текущая неделя ВКЛЮЧЕНА (W-32 в списке)."""
    today = date(2026, 8, 3)
    _, end_dt, weeks = compute_window(today)
    check("monday_w32/end_includes_current", end_dt.date() == date(2026, 8, 10),
          f"got {end_dt.date()}")
    check("monday_w32/last_label", weeks[-1].label == "W-32",
          f"got {weeks[-1].label}")


def test_compute_window_sunday_w32() -> None:
    """2026-08-09 (Sun, W-32 ends) — current_monday=Aug 3, конец Aug 10."""
    today = date(2026, 8, 9)
    start_dt, end_dt, weeks = compute_window(today)
    check("sunday_w32/start_dt", start_dt.date() == date(2026, 7, 6),
          f"got {start_dt.date()}")
    check("sunday_w32/end_dt", end_dt.date() == date(2026, 8, 10),
          f"got {end_dt.date()}")


def test_compute_window_next_monday() -> None:
    """2026-08-10 (Mon, W-33 starts) — current_monday=Aug 10, окно двигается
    на неделю вперёд: W-29..W-33 (5 недель, current включена)."""
    today = date(2026, 8, 10)
    start_dt, end_dt, weeks = compute_window(today)
    check("next_monday/start_dt", start_dt.date() == date(2026, 7, 13),
          f"got {start_dt.date()}")
    check("next_monday/end_dt", end_dt.date() == date(2026, 8, 17),
          f"got {end_dt.date()}")
    check("next_monday/last_label", weeks[-1].label == "W-33",
          f"got {weeks[-1].label}")


def test_compute_window_year_boundary() -> None:
    """2026-01-05 (Mon, W-2) — current_monday=Jan 5, окно W-49..W-2 (через
    границу года, current W-2 включена). Проверяем isocalendar."""
    today = date(2026, 1, 5)
    _, end_dt, weeks = compute_window(today)
    check("year/end_includes_current", end_dt.date() == date(2026, 1, 12),
          f"got {end_dt.date()}")
    # earliest_monday = Jan 5 − 4 weeks = 2025-12-08.
    check("year/first_monday", weeks[0].monday == date(2025, 12, 8),
          f"got {weeks[0].monday}")
    check("year/last_label", weeks[-1].label == "W-2",
          f"got {weeks[-1].label}")


def test_compute_window_monday_sunday_inclusive() -> None:
    """Sanity: каждая неделя — Пн..Вс, ровно 7 дней (sun − mon = 6)."""
    today = date(2026, 8, 7)
    _, _, weeks = compute_window(today)
    for w in weeks:
        check(f"span/{w.label}/mon_sun",
              (w.sunday - w.monday).days == 6,
              f"monday={w.monday} sunday={w.sunday}")


# ---- project_from_workspace -----------------------------------------------

def test_project_with_date_prefix() -> None:
    check("proj/with_prefix/win",
          project_from_workspace(r"C:\Projects\kamkb\0807_db-contingent") == "db-contingent",
          "")
    check("proj/with_prefix/unix",
          project_from_workspace("C:/Projects/kamkb/0807_db-contingent") == "db-contingent",
          "")


def test_project_no_prefix() -> None:
    check("proj/no_prefix",
          project_from_workspace("C:/Projects/humans/foo") == "foo",
          "")
    check("proj/no_prefix_documents",
          project_from_workspace(r"C:\Users\user\Documents") == "Documents",
          "")


def test_project_three_digit_no_strip() -> None:
    """Префикс ровно 4 цифры + '_'. 999_ не должен стрипаться (это не год)."""
    check("proj/three_digit",
          project_from_workspace("C:/x/999_thing") == "999_thing",
          "")


def test_project_year_only_at_start() -> None:
    """Если 4 цифры НЕ в начале, не стрипаем."""
    check("proj/year_in_middle",
          project_from_workspace("C:/x/foo_2025_bar") == "foo_2025_bar",
          "")


def test_project_empty() -> None:
    check("proj/empty", project_from_workspace("") is None, "")
    check("proj/none", project_from_workspace(None) is None, "")
    # Trailing slash → name = "" → None.
    check("proj/root", project_from_workspace("C:/") is None, "")


# ---- format_tokens --------------------------------------------------------

def test_format_tokens_under_k() -> None:
    check("tok/0", format_tokens(0) == "0", "")
    check("tok/123", format_tokens(123) == "123", "")
    check("tok/999", format_tokens(999) == "999", "")


def test_format_tokens_k() -> None:
    check("tok/1000", format_tokens(1000) == "1K", "")
    check("tok/1500", format_tokens(1500) == "1.5K", "")
    check("tok/941758", format_tokens(941_758) == "941.8K", "")
    check("tok/1234", format_tokens(1234) == "1.2K", "")


def test_format_tokens_m() -> None:
    check("tok/1M", format_tokens(1_000_000) == "1M", "")
    check("tok/5.93M", format_tokens(5_930_000) == "5.93M", "")
    check("tok/12.3M", format_tokens(12_300_000) == "12.3M", "")


def test_format_tokens_b() -> None:
    check("tok/1B", format_tokens(1_000_000_000) == "1B", "")
    check("tok/1.5B", format_tokens(1_500_000_000) == "1.5B", "")


def test_format_tokens_negative() -> None:
    check("tok/neg", format_tokens(-5) == "0", "")


# ---- format_duration ------------------------------------------------------

def test_format_duration_seconds() -> None:
    check("dur/0ms", format_duration(0) == "< 1m", "")
    check("dur/30s", format_duration(30_000) == "< 1m", "")
    check("dur/59s", format_duration(59_000) == "< 1m", "")


def test_format_duration_minutes() -> None:
    check("dur/1m", format_duration(60_000) == "1m", "")
    check("dur/45m", format_duration(45 * 60_000) == "45m", "")
    check("dur/59m", format_duration(59 * 60_000) == "59m", "")


def test_format_duration_hours() -> None:
    # 1h = 3_600_000 ms.
    check("dur/1h", format_duration(3_600_000) == "1h", "")
    # 1h 4m = 3_840_000 ms (matches screenshot).
    check("dur/1h4m", format_duration(3_840_000) == "1h 4m", "")
    # Round-down: 1h 59m 59s → 1h 59m.
    check("dur/1h59m59s", format_duration(3_600_000 + 59 * 60_000 + 59_000) == "1h 59m", "")


def test_format_duration_days() -> None:
    # 1d = 86_400_000 ms.
    check("dur/1d", format_duration(86_400_000) == "1d", "")
    check("dur/2d3h", format_duration(2 * 86_400_000 + 3 * 3_600_000) == "2d 3h", "")


# ---- format_date_cell -----------------------------------------------------

def test_format_date_same() -> None:
    d = date(2026, 8, 7)
    check("date/same", format_date_cell(d, d) == "2026-08-07", "")


def test_format_date_2_days() -> None:
    check("date/2days", format_date_cell(date(2026, 8, 5), date(2026, 8, 7))
          == "2026-08-05 → 2026-08-07", "")


def test_format_date_8_days() -> None:
    check("date/8days", format_date_cell(date(2026, 8, 5), date(2026, 8, 12))
          == "2026-08-05 → 2026-08-12 (8d)", "")


# ---- collect_sessions (in-memory SQLite) -----------------------------------

def _make_db() -> sqlite3.Connection:
    """In-memory DB со схемой как в runtime-state.sqlite."""
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
    """MSK datetime → epoch ms."""
    dt = datetime(d.year, d.month, d.day, h, m, tzinfo=MSK)
    return int(dt.timestamp() * 1000)


def test_collect_empty() -> None:
    con = _make_db()
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/empty", rows == [], f"got {rows}")


def test_collect_single_session() -> None:
    """Одна сессия: 2 user-сообщения + 1 assistant + 100 tokens."""
    con = _make_db()
    # 2026-08-04 10:00..11:00 MSK.
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.executemany(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        [
            ("s1", "m1", "user", base),
            ("s1", "m2", "assistant", base + 60_000),
            ("s1", "m3", "user", base + 3_600_000),  # +1h
        ],
    )
    con.executemany(
        "INSERT INTO local_runtime_token_usage (session_id, ts, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        [
            ("s1", base, 30, 20),
            ("s1", base + 60_000, 30, 20),
        ],
    )
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json, updated_at_ms) "
        "VALUES (?, ?, ?)",
        ("s1", '{"title": "Test", "workspaceDir": "C:/x/0807_foo", "status": "finished"}', base),
    )

    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/single/count", len(rows) == 1, f"got {len(rows)}")
    if rows:
        r = rows[0]
        check("collect/single/title", r.title == "Test", f"got {r.title!r}")
        check("collect/single/project", r.project == "foo", f"got {r.project!r}")
        check("collect/single/requests", r.requests == 2, f"got {r.requests}")
        check("collect/single/tokens", r.tokens == 100, f"got {r.tokens}")
        check("collect/single/duration_ms", r.duration_ms == 3_600_000,
              f"got {r.duration_ms}")
        check("collect/single/start_msk", r.start_msk == date(2026, 8, 4),
              f"got {r.start_msk}")
        check("collect/single/end_msk", r.end_msk == date(2026, 8, 4),
              f"got {r.end_msk}")
        check("collect/single/is_active", r.is_active is False, f"got {r.is_active}")


def test_collect_filters_by_window() -> None:
    """Сессия ВНЕ окна не попадает в результат."""
    con = _make_db()
    # Outside: 2026-08-04 10:00 MSK.
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_outside", "m1", "user", base),
    )
    # Window: 2026-08-05..2026-08-11.
    win_start = _msk_to_ms(date(2026, 8, 5))
    win_end = _msk_to_ms(date(2026, 8, 12))
    rows = collect_sessions(con, win_start, win_end)
    check("collect/window/excludes_outside", rows == [], f"got {rows}")


def test_collect_includes_current_week_session() -> None:
    """Сессия в текущей (5-й) неделе ВКЛЮЧАЕТСЯ — главное изменение после
    расширения окна. main() режет по now_msk, имитируем это в тесте."""
    con = _make_db()
    # "Сейчас" = 2026-08-07 14:00 MSK. Сессия в 12:00 того же дня.
    now_ms = _msk_to_ms(date(2026, 8, 7), 14, 0)
    sess_ms = _msk_to_ms(date(2026, 8, 7), 12, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_current", "m1", "user", sess_ms),
    )
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json, updated_at_ms) "
        "VALUES (?, ?, ?)",
        ("s_current", '{"status": "started"}', sess_ms),
    )
    # Окно: [earliest_monday 00:00, now 14:00). Сессия в 12:00 попадает.
    win_start = _msk_to_ms(date(2026, 7, 6))
    rows = collect_sessions(con, win_start, now_ms)
    check("collect/current_week/included", len(rows) == 1, f"got {len(rows)}")
    if rows:
        check("collect/current_week/is_active", rows[0].is_active is True, "")


def test_collect_excludes_future_dated_in_current_week() -> None:
    """Future-dated строка (например, из-за сдвига часов) НЕ попадает, даже
    если её created_at_ms внутри текущей недели. main() режет по now_msk."""
    con = _make_db()
    now_ms = _msk_to_ms(date(2026, 8, 7), 14, 0)
    # Сессия "завтра" относительно now — 2026-08-08 10:00.
    future_ms = _msk_to_ms(date(2026, 8, 8), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_future", "m1", "user", future_ms),
    )
    win_start = _msk_to_ms(date(2026, 7, 6))
    rows = collect_sessions(con, win_start, now_ms)
    check("collect/future/excluded", rows == [], f"got {rows}")


def test_collect_active_marker() -> None:
    """status='started' → is_active=True."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_active", "m1", "user", base),
    )
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json, updated_at_ms) "
        "VALUES (?, ?, ?)",
        ("s_active", '{"status": "started"}', base),
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/active/exists", len(rows) == 1, f"got {len(rows)}")
    if rows:
        check("collect/active/flag", rows[0].is_active is True, f"got {rows[0].is_active}")


def test_collect_no_user_messages() -> None:
    """Сессия с 0 user-сообщений: requests=0, всё равно попадает (есть activity)."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_tool_only", "m1", "tool", base),
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/tool_only/exists", len(rows) == 1, f"got {len(rows)}")
    if rows:
        check("collect/tool_only/requests", rows[0].requests == 0,
              f"got {rows[0].requests}")


def test_collect_sort_desc() -> None:
    """Сессии сортируются по max_ms desc (свежие сверху)."""
    con = _make_db()
    # s_old: Aug 1 10:00, s_mid: Aug 5 10:00, s_new: Aug 9 10:00.
    times = [
        ("s_old", date(2026, 8, 1), 10),
        ("s_mid", date(2026, 8, 5), 10),
        ("s_new", date(2026, 8, 9), 10),
    ]
    for sid, d, h in times:
        ms = _msk_to_ms(d, h)
        con.execute(
            "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
            "VALUES (?, ?, ?, ?, '{}')",
            (sid, "m1", "user", ms),
        )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/sort/count", len(rows) == 3, f"got {len(rows)}")
    if len(rows) == 3:
        ids = [r.session_id for r in rows]
        check("collect/sort/order", ids == ["s_new", "s_mid", "s_old"],
              f"got {ids}")


def test_collect_no_tokens() -> None:
    """Сессия с messages, но без token_usage → tokens=0."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_no_tok", "m1", "user", base),
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/no_tokens/tokens", rows[0].tokens == 0 if rows else False,
          f"got {rows}")


def test_collect_no_session_meta() -> None:
    """Сессия без записи в local_runtime_sessions → title=None, project=None,
    is_active=False, но строка всё равно есть (есть activity в messages)."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_orphan", "m1", "user", base),
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/orphan/exists", len(rows) == 1, f"got {len(rows)}")
    if rows:
        check("collect/orphan/title", rows[0].title is None, "")
        check("collect/orphan/project", rows[0].project is None, "")
        check("collect/orphan/is_active", rows[0].is_active is False, "")


def test_collect_broken_json() -> None:
    """Битый record_json → не падаем, мета=None-ы."""
    con = _make_db()
    base = _msk_to_ms(date(2026, 8, 4), 10, 0)
    con.execute(
        "INSERT INTO local_runtime_message_rows (session_id, msg_id, role, created_at_ms, data_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("s_broken", "m1", "user", base),
    )
    con.execute(
        "INSERT INTO local_runtime_sessions (session_id, record_json, updated_at_ms) "
        "VALUES (?, ?, ?)",
        ("s_broken", "{not valid json", base),
    )
    rows = collect_sessions(con, 0, 9_999_999_999_999)
    check("collect/broken/exists", len(rows) == 1, f"got {len(rows)}")
    if rows:
        check("collect/broken/title", rows[0].title is None, "")


# ---- render_html (smoke) ---------------------------------------------------

def test_render_empty() -> None:
    """Пустой список → валидный HTML, нет сессий в footer."""
    now = datetime(2026, 8, 7, 21, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html_doc = render_html([], now, weeks)
    check("render/empty/has_card", '<div class="card">' in html_doc, "")
    check("render/empty/has_title", "session dashboard" in html_doc, "")
    check("render/empty/no_sessions", "0 sessions" in html_doc, "")
    check("render/empty/no_active_badge",
          "active</span>" not in html_doc.replace("обновлено", ""),
          "active badge should not appear when no rows")


def test_render_with_active_row() -> None:
    """Active-сессия → строка с .active и бейджем."""
    now = datetime(2026, 8, 7, 21, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    rows = [
        SessionRow(
            session_id="abc1234567890",
            title="Active work",
            project="foo",
            workspace_dir="C:/x/0807_foo",
            start_msk=date(2026, 8, 5),
            end_msk=date(2026, 8, 6),
            max_ms=int(now.timestamp() * 1000),
            duration_ms=3_600_000,
            tokens=1234,
            requests=2,
            is_active=True,
        ),
    ]
    html_doc = render_html(rows, now, weeks)
    check("render/active/has_class", '<tr class="active">' in html_doc, "")
    check("render/active/has_badge", '<span class="badge">active</span>' in html_doc, "")
    check("render/active/title_escaped", "Active work" in html_doc, "")
    check("render/active/project", ">foo</td>" in html_doc, "")
    check("render/active/tokens", ">1.2K</td>" in html_doc, "")
    check("render/active/duration", ">1h</td>" in html_doc, "")
    check("render/active/date_range", "2026-08-05 → 2026-08-06" in html_doc, "")
    check("render/active/count", "1 sessions" in html_doc, "")
    check("render/active/active_count", "1 active" in html_doc, "")


def test_render_title_fallback() -> None:
    """Пустой title → fallback на хвост session_id."""
    now = datetime(2026, 8, 7, 21, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    rows = [
        SessionRow(
            session_id="abcdef1234567890",
            title=None,
            project="x",
            workspace_dir=None,
            start_msk=date(2026, 8, 5),
            end_msk=date(2026, 8, 5),
            max_ms=int(now.timestamp() * 1000),
            duration_ms=60_000,
            tokens=0,
            requests=0,
            is_active=False,
        ),
    ]
    html_doc = render_html(rows, now, weeks)
    # session_id="abcdef1234567890" → last 8 chars = "34567890".
    check("render/fallback/sid_tail", "…34567890" in html_doc, "")


def test_render_xss_protection() -> None:
    """HTML-инъекция в title → экранируется."""
    now = datetime(2026, 8, 7, 21, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    rows = [
        SessionRow(
            session_id="s1",
            title="<script>alert(1)</script>",
            project="x",
            workspace_dir=None,
            start_msk=date(2026, 8, 5),
            end_msk=date(2026, 8, 5),
            max_ms=int(now.timestamp() * 1000),
            duration_ms=60_000,
            tokens=0,
            requests=0,
            is_active=False,
        ),
    ]
    html_doc = render_html(rows, now, weeks)
    check("render/xss/script_escaped",
          "<script>alert(1)</script>" not in html_doc
          and "&lt;script&gt;" in html_doc, "")


# ---- main ------------------------------------------------------------------

def main() -> int:
    test_funcs = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in test_funcs:
        fn()
    # Печать PASS/FAIL.
    ok_count = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    if failures:
        print(f"FAIL — {len(failures)}/{total} checks failed:")
        for f in failures:
            print(f)
        return 1
    print(f"PASS — {ok_count}/{total} checks across {len(test_funcs)} test functions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
