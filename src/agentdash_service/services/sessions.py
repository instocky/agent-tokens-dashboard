"""SQL + assembly for /api/v1/sessions/snapshot.

All read-only. Per-session aggregates over the rolling window.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from ..config import settings, TZ
from ..models.sessions import SessionsSnapshot, SessionsWindow, SessionRow
from . import time as time_svc
from .cost import compute_cost
from .project import project_from_workspace


# ---- SQL helpers (reuse patterns from services/projects.py) -------------


def _sessions_in_window(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> dict[str, tuple[int, int, int]]:
    sql = """
        SELECT session_id,
               MIN(created_at_ms) AS min_ms,
               MAX(created_at_ms) AS max_ms,
               SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_msgs
        FROM local_runtime_message_rows
        WHERE created_at_ms >= ? AND created_at_ms < ?
        GROUP BY session_id
    """
    out: dict[str, tuple[int, int, int]] = {}
    for sid, mn, mx, user in con.execute(sql, (start_ts_ms, end_ts_ms)):
        out[str(sid)] = (int(mn), int(mx), int(user))
    return out


def _tokens_per_session(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> dict[str, tuple[int, int]]:
    sql = """
        SELECT session_id,
               COALESCE(SUM(input_tokens), 0) AS in_t,
               COALESCE(SUM(output_tokens), 0) AS out_t
        FROM local_runtime_token_usage
        WHERE ts >= ? AND ts < ?
        GROUP BY session_id
    """
    out: dict[str, tuple[int, int]] = {}
    for sid, in_t, out_t in con.execute(sql, (start_ts_ms, end_ts_ms)):
        out[str(sid)] = (int(in_t), int(out_t))
    return out


def _sessions_meta(
    con: sqlite3.Connection, sids: list[str]
) -> dict[str, dict[str, Any]]:
    if not sids:
        return {}
    placeholders = ",".join("?" for _ in sids)
    sql = f"SELECT session_id, record_json FROM local_runtime_sessions WHERE session_id IN ({placeholders})"
    out: dict[str, dict[str, Any]] = {}
    try:
        for sid, rec_json in con.execute(sql, sids):
            try:
                rec = json.loads(rec_json) if rec_json else {}
                out[str(sid)] = rec if isinstance(rec, dict) else {}
            except (json.JSONDecodeError, TypeError):
                out[str(sid)] = {}
    except sqlite3.OperationalError:
        return {}
    return out


# ---- Public entry --------------------------------------------------------


def build_snapshot(
    con: sqlite3.Connection, now: datetime
) -> SessionsSnapshot:
    """Build the full /api/v1/sessions/snapshot response."""
    today = now.date()
    start_ms = time_svc.start_of_window_ms(today, TZ, settings.week_count)
    end_ms = time_svc.end_of_now_ms(now)

    sessions = _sessions_in_window(con, start_ms, end_ms)
    if not sessions:
        start_monday, _ = time_svc.current_week_window(today, settings.week_count)
        return SessionsSnapshot(
            now_msk=now.isoformat(),
            window=SessionsWindow(start=start_monday.isoformat(), end=today.isoformat()),
            sessions=[],
        )

    tokens = _tokens_per_session(con, start_ms, end_ms)
    meta = _sessions_meta(con, list(sessions.keys()))

    rows: list[SessionRow] = []
    for sid, (mn, mx, user) in sessions.items():
        rec = meta.get(sid, {})
        title = rec.get("title") if isinstance(rec.get("title"), str) else None
        workspace_dir = rec.get("workspaceDir") if isinstance(rec.get("workspaceDir"), str) else None
        status = rec.get("status")
        project = project_from_workspace(workspace_dir)

        start_dt = datetime.fromtimestamp(mn / 1000, tz=TZ)
        end_dt = datetime.fromtimestamp(mx / 1000, tz=TZ)
        duration_ms = mx - mn

        in_t, out_t = tokens.get(sid, (0, 0))
        rows.append(SessionRow(
            session_id=sid,
            title=title,
            project=project,
            workspace_dir=workspace_dir,
            start_msk=start_dt.date().isoformat(),
            end_msk=end_dt.date().isoformat(),
            max_ms=mx,
            duration_ms=duration_ms,
            tokens=in_t + out_t,
            input_tokens=in_t,
            output_tokens=out_t,
            cost_usd=compute_cost(in_t, out_t, settings),
            requests=user,
            is_active=(status == "started"),
        ))

    # Sort by max_ms desc (most recent first)
    rows.sort(key=lambda r: r.max_ms, reverse=True)

    start_monday, _ = time_svc.current_week_window(today, settings.week_count)
    return SessionsSnapshot(
        now_msk=now.isoformat(),
        window=SessionsWindow(start=start_monday.isoformat(), end=today.isoformat()),
        sessions=rows,
    )
