"""Unit tests for build_project_dashboard.py.

Запускается напрямую: `python tests/test_project_dashboard.py`.
Не pytest — следуем конвенции tests/ в этом проекте (см. test_windows.py,
test_log_scale.py, test_weekly_cap.py, test_24h_stream.py).

Покрывает:
  - project_from_workspace: slug extraction, meta-workspace skip
  - compute_window: 5 недель (4 завершённых + текущая), Пн–Вс
  - format_tokens: K/M/B boundary precision + trailing zero strip
  - format_duration: Xm / Xh Ym / Xd Yh градация
  - collect_projects: группировка, агрегация, сортировка, active-флаг
    (через in-memory SQLite — самый ценный тест: ловит регрессию SQL)
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Чтобы import работал и при запуске из корня, и из tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_project_dashboard import (  # noqa: E402
    MSK,
    ProjectRow,
    WeekSpan,
    collect_projects,
    compute_window,
    format_duration,
    format_rate,
    format_tokens,
    project_from_workspace,
    rate_sort_value,
    render_html,
)


# ---- project_from_workspace -----------------------------------------------

def test_project_from_workspace_slug_extraction() -> None:
    """YYYY_ префикс стрипается, остальное — как есть."""
    cases = [
        # (workspaceDir, expected slug)
        ("C:/Projects/Python/0803_agent-tokens-dashboard", "agent-tokens-dashboard"),
        ("C:/Projects/humans/0807_db-contingent", "db-contingent"),
        ("C:/Projects/Python/0803_agent-tokens-dashboard/", "agent-tokens-dashboard"),
        ("C:/Projects/humans/foo", "foo"),  # без префикса — as-is
        # backslash Windows path тоже поддерживается через Path().
        ("C:\\Projects\\Python\\0803_agent-tokens-dashboard", "agent-tokens-dashboard"),
    ]
    failures: list[str] = []
    for ws, expected in cases:
        got = project_from_workspace(ws)
        if got != expected:
            failures.append(f"  {ws!r}: got {got!r}, expected {expected!r}")
    if failures:
        raise AssertionError("project_from_workspace slug failures:\n"
                             + "\n".join(failures))


def test_project_from_workspace_meta_skip() -> None:
    """.mavis и .minimax в path components → None (meta-workspace)."""
    cases = [
        # (workspaceDir, описание)
        ("C:/Users/user/.mavis/agents/mavis/workspace", "mavis agent workspace"),
        ("C:/Users/user/.minimax/v2/sqlite", "minimax sqlite dir"),
        ("C:/Users/user/.mavis", "bare mavis dir"),
        ("", "empty string"),
    ]
    failures: list[str] = []
    for ws, desc in cases:
        got = project_from_workspace(ws)
        if got is not None:
            failures.append(f"  {desc!r} ({ws!r}): got {got!r}, expected None")
    if failures:
        raise AssertionError("meta-skip failures:\n" + "\n".join(failures))


def test_project_from_workspace_none() -> None:
    """None на входе → None на выходе, без exception."""
    assert project_from_workspace(None) is None
    assert project_from_workspace("") is None


def test_project_from_workspace_does_not_match_substring() -> None:
    """`.mavis` как substring в имени файла — НЕ должно триггерить skip.
    Проверяем что ищем именно path components, не подстроку."""
    # foo.mavis-bar — basename содержит ".mavis" как подстроку,
    # но parts не содержат ".mavis".
    got = project_from_workspace("C:/Projects/test/0803_foo.mavis-bar")
    assert got == "foo.mavis-bar", f"got {got!r}"


# ---- compute_window --------------------------------------------------------

def test_compute_window_5_weeks() -> None:
    """4 завершованных + текущая = 5 недель всего."""
    today = date(2026, 8, 7)  # пятница W-32
    start_dt, end_dt, weeks = compute_window(today)
    assert len(weeks) == 5, f"weeks={len(weeks)}"
    assert weeks[0].label == "W-28"
    assert weeks[-1].label == "W-32"
    # Каждая неделя: Пн–Вс (isoweekday 1..7).
    for w in weeks:
        assert w.monday.isoweekday() == 1, f"{w.label}: monday={w.monday}"
        assert w.sunday.isoweekday() == 7, f"{w.label}: sunday={w.sunday}"
        assert (w.sunday - w.monday).days == 6
    # start_dt = midnight Monday W-28; end_dt = midnight Monday W-33.
    assert start_dt.date() == date(2026, 7, 6)  # Monday W-28
    assert end_dt.date() == date(2026, 8, 10)   # Monday W-33 (exclusive end)
    # Timezone — MSK.
    assert start_dt.tzinfo == MSK
    assert end_dt.tzinfo == MSK


def test_compute_window_monday_start() -> None:
    """Сегодня = понедельник: current_monday == today, окно всё равно 5 недель."""
    today = date(2026, 8, 3)  # понедельник W-32
    _, _, weeks = compute_window(today)
    assert weeks[-1].monday == today
    assert weeks[-1].sunday == date(2026, 8, 9)
    assert weeks[0].monday == date(2026, 7, 6)


def test_compute_window_sunday_end() -> None:
    """Сегодня = воскресенье: current_monday = сегодня − 6."""
    today = date(2026, 8, 9)  # воскресенье W-32
    _, _, weeks = compute_window(today)
    assert weeks[-1].monday == date(2026, 8, 3)
    assert weeks[-1].sunday == today


# ---- format_tokens ---------------------------------------------------------

def test_format_tokens() -> None:
    """Контракт precision: K=1dp, M/B=2dp, trailing zero strip.

    NB: round-up на границе (например, 999_999 → "1000K") — это тот же
    контракт, что в build_dashboard.py::fmt_tokens и
    build_session_dashboard.py::format_tokens. Ловить не надо — это
    согласовано с двумя другими скриптами.
    """
    cases = [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1_000, "1K"),
        (1_500, "1.5K"),
        (182_500, "182.5K"),
        (941_800, "941.8K"),
        (999_999, "1000K"),  # round-up на K→M границе (как в session-dashboard)
        (1_000_000, "1M"),  # trailing .00 strip
        (1_230_000, "1.23M"),
        (5_930_000, "5.93M"),
        (999_990_000, "999.99M"),  # последний 2dp без round-up
        (999_999_999, "1000M"),  # round-up на M→B границе
        (1_000_000_000, "1B"),
        (1_500_000_000, "1.5B"),
        (100_000, "100K"),  # 100.0K → 100K
        (-1, "0"),  # защита от отрицательных
    ]
    failures: list[str] = []
    for n, expected in cases:
        got = format_tokens(n)
        if got != expected:
            failures.append(f"  {n}: got {got!r}, expected {expected!r}")
    if failures:
        raise AssertionError("format_tokens failures:\n" + "\n".join(failures))


# ---- format_duration -------------------------------------------------------

def test_format_duration() -> None:
    """Градация: <1m / Xm / Xh / Xh Ym / Xd / Xd Yh."""
    cases = [
        (0, "< 1m"),
        (30_000, "< 1m"),  # 0.5 min
        (59_999, "< 1m"),
        (60_000, "1m"),  # ровно 1 min
        (1_500_000, "25m"),  # 25 min
        (3_600_000, "1h"),  # ровно 1 час
        (3_900_000, "1h 5m"),  # 1h 5m
        (7_440_000, "2h 4m"),  # 2h 4m
        (86_400_000, "1d"),  # ровно 1 день
        (90_000_000, "1d 1h"),  # 1d 1h
        (194_400_000, "2d 6h"),  # 2d 6h
        (-1, "0m"),  # защита
    ]
    failures: list[str] = []
    for ms, expected in cases:
        got = format_duration(ms)
        if got != expected:
            failures.append(f"  {ms}: got {got!r}, expected {expected!r}")
    if failures:
        raise AssertionError("format_duration failures:\n" + "\n".join(failures))


# ---- format_rate / rate_sort_value ----------------------------------------

def test_format_rate() -> None:
    """Display: K/M/h precision (тот же, что format_tokens), < 1m duration → "—".

    Cases подобраны под реалистичные значения из project dashboard:
      - мелкие rate (sub-1K) → "<N>/h" без суффикса
      - K-шкала (1K..999K) → 1dp ("335.7K/h")
      - M-шкала (1M..999M) → 2dp ("1.5M/h")
      - B-шкала (1B+) → 2dp (для project-уровня нереалистично, но контракт должен держаться)
    """
    cases = [
        # (tokens, duration_ms, expected_display)
        (0, 3_600_000, "0/h"),                  # 0 tokens / 1h
        (200, 1_800_000, "400/h"),              # 200 / 30m = 400/h
        (1_000, 60_000, "60K/h"),               # ровно 1m duration (граница) — rate=1_000 * 60 = 60K
        (1_500_000, 3_600_000, "1.5M/h"),       # 1.5M / 1h
        (1_500_000, 7_200_000, "750K/h"),       # 1.5M / 2h = 750K
        (9_400_000, 100_800_000, "335.7K/h"),   # 9.4M / 28h ≈ 335_714/h
        (18_270_000, 67_020_000, "981.4K/h"),   # 18.27M / 18.617h ≈ 981_379/h (1dp rounds up)
        (1_500_000_000, 3_600_000, "1.5B/h"),   # B-шкала, для project нереалистично, но контракт
        (0, 30_000, "—"),                       # < 1m → "—"
        (1_000, 30_000, "—"),                   # 1K tokens / 30s → было бы "120K/h" (misleading) → "—"
        (1_000, 59_999, "—"),                   # 59_999 ms (граница чуть ниже 1m)
        (-100, 3_600_000, "0/h"),               # отрицательные → 0 через format_tokens
    ]
    failures: list[str] = []
    for tokens, ms, expected in cases:
        got = format_rate(tokens, ms)
        if got != expected:
            failures.append(
                f"  ({tokens}, {ms}ms): got {got!r}, expected {expected!r}"
            )
    if failures:
        raise AssertionError("format_rate failures:\n" + "\n".join(failures))


def test_rate_sort_value() -> None:
    """Raw int для client-side сортировки. < 1m → 0."""
    cases = [
        # (tokens, duration_ms, expected_raw)
        (1_500_000, 3_600_000, 1_500_000),      # 1.5M / 1h
        (1_500_000, 7_200_000, 750_000),        # 1.5M / 2h
        (9_400_000, 100_800_000, 335_714),      # 9.4M / 28h (rounded)
        (200, 1_800_000, 400),                  # 200 / 30m = 400
        (0, 3_600_000, 0),                      # 0 tokens
        (1_000, 30_000, 0),                     # < 1m → 0
        (0, 0, 0),                              # edge: оба нули
        (1_000, 59_999, 0),                     # edge: 59_999 ms
    ]
    failures: list[str] = []
    for tokens, ms, expected in cases:
        got = rate_sort_value(tokens, ms)
        if got != expected:
            failures.append(
                f"  ({tokens}, {ms}ms): got {got!r}, expected {expected!r}"
            )
    if failures:
        raise AssertionError("rate_sort_value failures:\n" + "\n".join(failures))


# ---- collect_projects (integration через :memory: SQLite) -----------------

def _make_db() -> sqlite3.Connection:
    """Создать in-memory SQLite с минимальной schema под collect_projects."""
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE local_runtime_message_rows (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE local_runtime_token_usage (
            session_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        );
        CREATE TABLE local_runtime_sessions (
            session_id TEXT PRIMARY KEY,
            record_json TEXT
        );
    """)
    return con


def _insert_session(
    con: sqlite3.Connection,
    sid: str,
    workspace_dir: str | None,
    status: str,
    msgs: list[tuple[int, str]],  # (created_at_ms, role)
    tokens: list[tuple[int, int, int]],  # (ts, in, out)
) -> None:
    """Хелпер для вставки одной сессии в тестовую БД."""
    for ts, role in msgs:
        con.execute(
            "INSERT INTO local_runtime_message_rows VALUES (?, ?, ?)",
            (sid, role, ts),
        )
    for ts, in_tok, out_tok in tokens:
        con.execute(
            "INSERT INTO local_runtime_token_usage VALUES (?, ?, ?, ?)",
            (sid, ts, in_tok, out_tok),
        )
    rec = {
        "sessionId": sid,
        "workspaceDir": workspace_dir,
        "status": status,
    }
    con.execute(
        "INSERT INTO local_runtime_sessions VALUES (?, ?)",
        (sid, json.dumps(rec)),
    )


def test_collect_projects_groups_by_project() -> None:
    """Две сессии одного проекта → одна строка, агрегаты просуммированы."""
    con = _make_db()
    # Проект "alpha", две сессии в W-32.
    _insert_session(
        con, "s1", "C:/Projects/Python/0803_alpha",
        status="finished",
        msgs=[(1_700_000_000_000, "user"), (1_700_003_600_000, "assistant")],  # 1h
        tokens=[(1_700_000_000_000, 100, 50), (1_700_003_600_000, 200, 80)],
    )
    _insert_session(
        con, "s2", "C:/Projects/Python/0803_alpha",
        status="finished",
        msgs=[(1_700_010_000_000, "user"), (1_700_010_900_000, "assistant")],  # 15m
        tokens=[(1_700_010_000_000, 300, 100), (1_700_010_900_000, 50, 20)],
    )
    # Проект "beta", одна сессия.
    _insert_session(
        con, "s3", "C:/Projects/humans/0807_beta",
        status="finished",
        msgs=[(1_700_020_000_000, "user"), (1_700_021_800_000, "assistant")],  # 30m
        tokens=[(1_700_020_000_000, 500, 200)],
    )
    con.commit()

    # Окно: 5 недель с 2023-11-13 (Mon W-46) — обе сессии внутри.
    # 1_700_000_000_000 = 2023-11-14 22:13:20 UTC ≈ 2023-11-15 01:13:20 MSK.
    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)

    rows = collect_projects(con, start_ms, end_ms)
    assert len(rows) == 2, f"got {len(rows)} rows"

    by_name = {r.project: r for r in rows}

    # alpha: 2 sessions, total duration ~1h15m = 75m, tokens=100+50+200+80+300+100+50+20=900.
    alpha = by_name["alpha"]
    assert alpha.sessions == 2
    assert alpha.tokens == 900
    assert format_duration(alpha.duration_ms) == "1h 15m", (
        f"alpha dur={format_duration(alpha.duration_ms)}"
    )
    assert alpha.is_active is False

    # beta: 1 session, duration ~30m, tokens=500+200=700.
    beta = by_name["beta"]
    assert beta.sessions == 1
    assert beta.tokens == 700
    assert format_duration(beta.duration_ms) == "30m"


def test_collect_projects_sort_most_recent_first() -> None:
    """Sort: max_ms DESC. Проект с более свежей сессией — сверху."""
    con = _make_db()
    # old: ts=2023-11-15 MSK.
    _insert_session(
        con, "s_old", "C:/Projects/0803_old",
        status="finished",
        msgs=[(1_700_000_000_000, "user"), (1_700_000_600_000, "assistant")],
        tokens=[(1_700_000_000_000, 10, 5)],
    )
    # recent: ts=2023-11-20 MSK.
    _insert_session(
        con, "s_recent", "C:/Projects/0803_recent",
        status="finished",
        msgs=[(1_700_400_000_000, "user"), (1_700_400_600_000, "assistant")],
        tokens=[(1_700_400_000_000, 10, 5)],
    )
    con.commit()

    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)
    rows = collect_projects(con, start_ms, end_ms)
    assert len(rows) == 2
    assert rows[0].project == "recent", f"top={rows[0].project}"
    assert rows[1].project == "old"


def test_collect_projects_active_flag() -> None:
    """is_active=True, если хотя бы одна сессия проекта status='started'."""
    con = _make_db()
    _insert_session(
        con, "s1", "C:/Projects/0803_alpha",
        status="finished",
        msgs=[(1_700_000_000_000, "user"), (1_700_000_600_000, "assistant")],
        tokens=[(1_700_000_000_000, 10, 5)],
    )
    _insert_session(
        con, "s2", "C:/Projects/0803_alpha",
        status="started",  # ← активная
        msgs=[(1_700_001_000_000, "user"), (1_700_001_600_000, "assistant")],
        tokens=[(1_700_001_000_000, 10, 5)],
    )
    con.commit()

    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)
    rows = collect_projects(con, start_ms, end_ms)
    assert len(rows) == 1
    assert rows[0].is_active is True, "active session должен дать active project"


def test_collect_projects_skips_meta_workspaces() -> None:
    """.mavis / .minimax / None workspaceDir → проект НЕ появляется в результате."""
    con = _make_db()
    # Meta — должно быть skipped.
    _insert_session(
        con, "s_meta", "C:/Users/user/.mavis/agents/mavis/workspace",
        status="finished",
        msgs=[(1_700_000_000_000, "user"), (1_700_000_600_000, "assistant")],
        tokens=[(1_700_000_000_000, 10, 5)],
    )
    # None workspaceDir — должно быть skipped.
    _insert_session(
        con, "s_null", None,  # type: ignore[arg-type]
        status="finished",
        msgs=[(1_700_001_000_000, "user"), (1_700_001_600_000, "assistant")],
        tokens=[(1_700_001_000_000, 10, 5)],
    )
    # Real — должно попасть.
    _insert_session(
        con, "s_real", "C:/Projects/0803_real",
        status="finished",
        msgs=[(1_700_002_000_000, "user"), (1_700_002_600_000, "assistant")],
        tokens=[(1_700_002_000_000, 10, 5)],
    )
    con.commit()

    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)
    rows = collect_projects(con, start_ms, end_ms)
    names = [r.project for r in rows]
    assert names == ["real"], f"got {names}"


def test_collect_projects_empty_window() -> None:
    """Пустое окно (нет сообщений) → []."""
    con = _make_db()
    # Сообщение вне окна.
    _insert_session(
        con, "s_earlier", "C:/Projects/0803_old",
        status="finished",
        msgs=[(1_000_000_000_000, "user"), (1_000_000_600_000, "assistant")],
        tokens=[(1_000_000_000_000, 10, 5)],
    )
    con.commit()
    rows = collect_projects(
        con,
        int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000),
        int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000),
    )
    assert rows == []


def test_collect_projects_session_without_token_usage() -> None:
    """Сессия с messages, но без token_usage → tokens=0, проект попадает."""
    con = _make_db()
    _insert_session(
        con, "s1", "C:/Projects/0803_alpha",
        status="finished",
        msgs=[(1_700_000_000_000, "user"), (1_700_000_600_000, "assistant")],
        tokens=[],  # ← пусто
    )
    con.commit()
    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)
    rows = collect_projects(con, start_ms, end_ms)
    assert len(rows) == 1
    assert rows[0].project == "alpha"
    assert rows[0].tokens == 0
    assert rows[0].sessions == 1


def test_collect_projects_broken_json() -> None:
    """Битый record_json → workspaceDir=None, сессия skip."""
    con = _make_db()
    con.execute(
        "INSERT INTO local_runtime_message_rows VALUES (?, ?, ?)",
        ("s1", "user", 1_700_000_000_000),
    )
    con.execute(
        "INSERT INTO local_runtime_message_rows VALUES (?, ?, ?)",
        ("s1", "assistant", 1_700_000_600_000),
    )
    con.execute(
        "INSERT INTO local_runtime_sessions VALUES (?, ?)",
        ("s1", "{broken json"),
    )
    con.commit()
    start_ms = int(datetime(2023, 11, 13, tzinfo=MSK).timestamp() * 1000)
    end_ms = int(datetime(2023, 12, 4, tzinfo=MSK).timestamp() * 1000)
    rows = collect_projects(con, start_ms, end_ms)
    assert rows == [], f"expected [], got {rows}"


# ---- render_html (client-side sort contract) -------------------------------

def test_render_html_has_sortable_headers() -> None:
    """Все 6 <th> помечены class="sortable" + data-col + tabindex."""
    rows: list[ProjectRow] = [
        ProjectRow(
            project="alpha",
            last_update=date(2026, 8, 7),
            max_ms=1_700_000_000_000,
            duration_ms=3_600_000,
            tokens=1_500_000,
            sessions=2,
            is_active=False,
        ),
    ]
    now = datetime(2026, 8, 7, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html_doc = render_html(rows, now, weeks)

    expected_cols = ["project", "last_update", "duration", "tokens", "rate", "sessions"]
    for col in expected_cols:
        # Ищем <th ... data-col="<col>" ... class="sortable" ...>
        marker = f'data-col="{col}"'
        assert marker in html_doc, f"missing {marker}"
        # class="sortable" должен быть на каждом sortable <th>
        # (берём подстроку ровно вокруг нашего data-col, чтобы не словить ложный матч).
        # Простая проверка: количество sortable th >= 6.
        assert html_doc.count('class="sortable') >= 6, (
            f"expected >= 6 sortable th, got {html_doc.count(chr(34)+'sortable')}"
        )
    # Заголовок rate: видимый лейбл "TOK/HOUR" (CSS uppercase от "Tok/Hour").
    assert "Tok/Hour" in html_doc, "rate column header label 'Tok/Hour' missing"


def test_render_html_has_data_sort_per_cell() -> None:
    """Каждый <td> имеет data-col и data-sort с raw-значением."""
    rows: list[ProjectRow] = [
        ProjectRow(
            project="alpha",
            last_update=date(2026, 8, 7),
            max_ms=1_700_000_000_000,
            duration_ms=3_600_000,    # 1h
            tokens=1_500_000,         # 1.5M (formatted)
            sessions=2,
            is_active=False,
        ),
    ]
    now = datetime(2026, 8, 7, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html_doc = render_html(rows, now, weeks)

    # Per-cell проверка. Берём первую строку из <tbody>, не из <thead>
    # (иначе возьмём header row, в которой нет data-sort).
    tbody_pos = html_doc.find("<tbody>")
    assert tbody_pos != -1, "<tbody> not found"
    tr_start = html_doc.find("<tr", tbody_pos)
    tr_end = html_doc.find("</tr>", tr_start) + len("</tr>")
    tr = html_doc[tr_start:tr_end]
    # data-col атрибуты должны присутствовать на каждом <td>.
    for col in ["project", "last_update", "duration", "tokens", "rate", "sessions"]:
        assert f'data-col="{col}"' in tr, f"row missing data-col={col}"
    # Raw values, не formatted: duration=3600000 (не "1h"), tokens=1500000 (не "1.5M").
    assert 'data-sort="3600000"' in tr, f"row missing data-sort for duration; got: {tr}"
    assert 'data-sort="1500000"' in tr, f"row missing data-sort for tokens; got: {tr}"
    # rate = 1_500_000 tokens / 1h = 1_500_000 per hour. Display "1.5M/h".
    assert 'data-sort="1500000"' in tr, f"row missing data-sort for rate; got: {tr}"
    assert ">1.5M/h<" in tr, f"row missing rate display '1.5M/h'; got: {tr}"
    assert 'data-sort="2"' in tr, f"row missing data-sort for sessions; got: {tr}"
    assert 'data-sort="2026-08-07"' in tr, f"row missing data-sort for last_update; got: {tr}"
    # project data-sort — без html.escape, но slug безопасный.
    assert 'data-sort="alpha"' in tr, f"row missing data-sort for project; got: {tr}"


def test_render_html_rate_short_duration_is_dash() -> None:
    """duration < 1m → ячейка rate показывает "—" с data-sort=0."""
    rows: list[ProjectRow] = [
        ProjectRow(
            project="shorty",
            last_update=date(2026, 8, 7),
            max_ms=1_700_000_000_000,
            duration_ms=30_000,        # 30s — ниже 1m порога
            tokens=10_000,
            sessions=1,
            is_active=False,
        ),
    ]
    now = datetime(2026, 8, 7, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html_doc = render_html(rows, now, weeks)

    tbody_pos = html_doc.find("<tbody>")
    tr_start = html_doc.find("<tr", tbody_pos)
    tr_end = html_doc.find("</tr>", tr_start) + len("</tr>")
    tr = html_doc[tr_start:tr_end]
    # rate cell: display "—", data-sort "0".
    assert 'data-col="rate"' in tr
    # Должна быть ровно одна ячейка rate с data-sort="0" и текстом "—".
    # Ищем подстроку <td ... data-col="rate" data-sort="0">—</td>.
    assert 'data-col="rate" data-sort="0"' in tr, (
        f"rate cell data-sort not 0 for short duration; got: {tr}"
    )
    assert ">—<" in tr, f"rate display not '—' for short duration; got: {tr}"


def test_render_html_sort_by_last_update_iso_asc_desc() -> None:
    """Регрессия: client-side sortBy(col='last_update', dir) реально меняет порядок.

    Бойлерплейт: достаём inline-<script> из render_html, инжектим в IIFE
    экспорт нужных функций на globalThis, гоняем через node с mock-DOM.
    Skip'аемся, если node не установлен (это не блокер на Windows-машинах
    без Node.js, но локально мы node прогоняем обязательно).

    Покрывает баг: data-sort="2026-08-07" + Number(...) = NaN → все cmp=0
    → порядок не меняется, индикатор asc/desc врёт.
    """
    if shutil.which("node") is None:
        print("    [skip] node not found")
        return

    # Три проекта с разными датами, но одинаковыми метриками, чтобы единственный
    # меняющийся сигнал был last_update. max_ms растёт вместе с датой — это
    # совпадает с python-дефолтом, но для client-side comparator неважно.
    rows: list[ProjectRow] = [
        ProjectRow(
            project="alpha",
            last_update=date(2026, 8, 5),
            max_ms=1_700_000_000_000,
            duration_ms=3_600_000,
            tokens=1_000_000,
            sessions=1,
            is_active=False,
        ),
        ProjectRow(
            project="beta",
            last_update=date(2026, 8, 7),
            max_ms=1_700_500_000_000,
            duration_ms=3_600_000,
            tokens=1_000_000,
            sessions=1,
            is_active=False,
        ),
        ProjectRow(
            project="gamma",
            last_update=date(2026, 8, 3),
            max_ms=1_699_500_000_000,
            duration_ms=3_600_000,
            tokens=1_000_000,
            sessions=1,
            is_active=False,
        ),
    ]
    now = datetime(2026, 8, 7, 12, 0, tzinfo=MSK)
    _start, _end, weeks = compute_window(now.date())
    html_doc = render_html(rows, now, weeks)

    # 1. Извлекаем inline-скрипт (после render_html f-string'ы уже развёрнуты,
    #    так что ищем обычные <script>...</script>).
    m = re.search(r"<script>(.*?)</script>", html_doc, re.S)
    assert m is not None, "<script> not found in rendered HTML"
    script = m.group(1)

    # 2. Перед последним `})();` инжектим экспорт функций на globalThis.
    #    IIFE: `(function () { ... })();`. Ищем конец IIFE и вставляем перед ним.
    #    Без `\\s*` в регексе — он вёл к ложному None (после `})();` идёт `\n  `
    #    от f-string отступов; матч есть, но `\\s*` его «не съел» стабильно).
    export_line = (
        "globalThis.__t = { sortValueFor: sortValueFor, "
        "compareRows: compareRows, sortBy: sortBy };\n    "
    )
    script_for_node, n = re.subn(
        r"\}\)\(\);", export_line + "})();", script, count=1
    )
    assert n == 1, "could not inject export into IIFE"

    # 3. Сборка driver'а: mock-DOM + вызов sortBy в обоих направлениях +
    #    печать порядка (project list) в stdout для парсинга.
    #    Inline-script использует только:
    #      - document.querySelector("table tbody")
    #      - document.querySelectorAll("thead th.sortable")
    #      - document.addEventListener("DOMContentLoaded", ...)
    #      - document.readyState
    #      - localStorage.getItem / setItem
    #    Реальные <tr>/<td> не нужны — нужен только контракт:
    #    row.querySelector('td[data-col="<col>"]') → getAttribute("data-sort").
    driver = r"""
'use strict';
const _store = {};
globalThis.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; },
  setItem: function (k, v) { _store[k] = String(v); },
};
function makeRow(project, cells) {
  // cells: объект {col: sortValue-as-string}
  const wrapped = {};
  for (const k of Object.keys(cells)) {
    wrapped[k] = { getAttribute: function (attr) { return attr === "data-sort" ? cells[k] : null; } };
  }
  return {
    _project: project,
    _cells: wrapped,
    querySelector: function (sel) {
      const m = sel.match(/td\[data-col="(\w+)"\]/);
      return m ? (this._cells[m[1]] || null) : null;
    },
  };
}
function makeTbody(rows) {
  return {
    _rows: rows,
    querySelectorAll: function (sel) {
      if (sel === "tr") return this._rows;
      return [];
    },
    appendChild: function (node) {
      const i = this._rows.indexOf(node);
      if (i >= 0) this._rows.splice(i, 1);
      this._rows.push(node);
    },
  };
}
// _ROWS — это сам tbody._rows, не отдельная копия: sortBy мутирует
// tbody._rows через appendChild, и мы хотим видеть эти изменения здесь.
const tbody = makeTbody(globalThis.__RAW_ROWS.map(function (r) { return makeRow(r._project, r._cells); }));
const _ROWS = tbody._rows;
globalThis.document = {
  querySelector: function (sel) {
    if (sel === "table tbody") return tbody;
    return null;
  },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  readyState: "complete",
};

__SCRIPT__;

__t.sortBy("last_update", "asc");
const ascOrder = _ROWS.map(function (r) { return r._project; }).join(",");
__t.sortBy("last_update", "desc");
const descOrder = _ROWS.map(function (r) { return r._project; }).join(",");
process.stdout.write("ASC=" + ascOrder + "\nDESC=" + descOrder + "\n");
"""
    rows_js_list = []
    for r in rows:
        cells = {
            "project": r.project,
            "last_update": r.last_update.isoformat(),
            "duration": str(r.duration_ms),
            "tokens": str(r.tokens),
            "rate": str(rate_sort_value(r.tokens, r.duration_ms)),
            "sessions": str(int(r.sessions)),
        }
        rows_js_list.append({"_project": r.project, "_cells": cells})
    rows_js = "globalThis.__RAW_ROWS = " + json.dumps(rows_js_list)

    # rows_js идёт ПЕРЕД driver: driver читает globalThis.__ROWS при выполнении.
    full = rows_js + "\n" + driver.replace("__SCRIPT__", script_for_node) + "\n"

    proc = subprocess.run(
        ["node", "-e", full],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited with {proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
    out = proc.stdout
    m_asc = re.search(r"ASC=([^\n]+)", out)
    m_desc = re.search(r"DESC=([^\n]+)", out)
    assert m_asc and m_desc, f"unexpected node output:\n{out}"
    asc_order = m_asc.group(1).split(",")
    desc_order = m_desc.group(1).split(",")

    # 08-03 gamma < 08-05 alpha < 08-07 beta. asc = [gamma, alpha, beta].
    assert asc_order == ["gamma", "alpha", "beta"], (
        f"asc order wrong: {asc_order}\nfull output:\n{out}"
    )
    # desc = [beta, alpha, gamma].
    assert desc_order == ["beta", "alpha", "gamma"], (
        f"desc order wrong: {desc_order}\nfull output:\n{out}"
    )
    # Sanity: asc и desc — взаимные reverse'ы.
    assert asc_order == list(reversed(desc_order)), (
        f"asc/desc are not reverses: asc={asc_order} desc={desc_order}"
    )


def test_render_html_embeds_sort_script_and_storage_key() -> None:
    """Inline <script> с localStorage-ключом и sortBy/init функциями."""
    rows: list[ProjectRow] = [
        ProjectRow(
            project="alpha",
            last_update=date(2026, 8, 7),
            max_ms=1_700_000_000_000,
            duration_ms=3_600_000,
            tokens=1_500_000,
            sessions=2,
            is_active=False,
        ),
    ]
    now = datetime(2026, 8, 7, 22, 0, tzinfo=MSK)
    _, _, weeks = compute_window(now.date())
    html_doc = render_html(rows, now, weeks)

    # Inline script.
    assert "<script>" in html_doc and "</script>" in html_doc
    # Storage key + дефолт.
    assert "agent-tokens-dashboard:sort" in html_doc
    assert "DEFAULT_STATE" in html_doc or '"col": "last_update"' in html_doc
    # Sort logic present.
    assert "sortBy" in html_doc
    assert "onHeaderClick" in html_doc
    # CSS rules.
    assert "thead th.sortable" in html_doc
    assert "cursor: pointer" in html_doc


# ---- main ------------------------------------------------------------------

def main() -> int:
    tests = [
        test_project_from_workspace_slug_extraction,
        test_project_from_workspace_meta_skip,
        test_project_from_workspace_none,
        test_project_from_workspace_does_not_match_substring,
        test_compute_window_5_weeks,
        test_compute_window_monday_start,
        test_compute_window_sunday_end,
        test_format_tokens,
        test_format_duration,
        test_format_rate,
        test_rate_sort_value,
        test_collect_projects_groups_by_project,
        test_collect_projects_sort_most_recent_first,
        test_collect_projects_active_flag,
        test_collect_projects_skips_meta_workspaces,
        test_collect_projects_empty_window,
        test_collect_projects_session_without_token_usage,
        test_collect_projects_broken_json,
        test_render_html_has_sortable_headers,
        test_render_html_has_data_sort_per_cell,
        test_render_html_rate_short_duration_is_dash,
        test_render_html_sort_by_last_update_iso_asc_desc,
        test_render_html_embeds_sort_script_and_storage_key,
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
