"""SQL + assembly for /api/v1/projects/snapshot.

All read-only. Per-project aggregates over the rolling window plus
per-project daily time series.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from ..config import TZ, settings
from ..models.projects import (
    ProjectRow,
    ProjectsSnapshot,
    ProjectsWindow,
    ProjectTimeSeries,
    ProjectTimeSeriesDay,
    ProjectTimeSeriesWeek,
)
from . import time as time_svc
from .cost import compute_cost
from .project import project_from_workspace

MSK_OFFSET = "+3 hours"


# ---- SQL helpers ---------------------------------------------------------


def _sessions_in_window(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> dict[str, tuple[int, int, int]]:
    """Returns {session_id: (min_ms, max_ms, user_msgs)} for sessions with messages in window."""
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
    """Returns {session_id: (input_sum, output_sum)} for sessions with token_usage in window."""
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
    """Read record_json for a list of session_ids. Returns {} on missing/broken JSON."""
    if not sids:
        return {}
    placeholders = ",".join("?" for _ in sids)
    sql = (
        f"SELECT session_id, record_json FROM local_runtime_sessions "
        f"WHERE session_id IN ({placeholders})"
    )
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


def _tokens_per_session_per_day(
    con: sqlite3.Connection, start_ts_ms: int, end_ts_ms: int
) -> dict[tuple[str, date], tuple[int, int]]:
    """Returns {(session_id, msk_date): (input, output)} for time series."""
    sql = f"""
        SELECT session_id,
               date(ts / 1000, 'unixepoch', '{MSK_OFFSET}') AS msk_date,
               SUM(input_tokens) AS in_sum,
               SUM(output_tokens) AS out_sum
        FROM local_runtime_token_usage
        WHERE ts >= ? AND ts < ?
        GROUP BY session_id, msk_date
    """
    out: dict[tuple[str, date], tuple[int, int]] = {}
    for sid, date_str, in_sum, out_sum in con.execute(sql, (start_ts_ms, end_ts_ms)):
        out[(str(sid), date.fromisoformat(date_str))] = (int(in_sum or 0), int(out_sum or 0))
    return out


# ---- Assembly helpers ----------------------------------------------------


def _build_time_series(
    sid_to_project: dict[str, str],
    sids: list[str],
    tokens_per_day: dict[tuple[str, date], tuple[int, int]],
    today: date,
) -> dict[str, ProjectTimeSeries]:
    """Build per-project weekly time series for the rolling window."""
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    week_count = settings.week_count

    # Aggregate daily tokens by project
    project_day_tokens: dict[tuple[str, date], tuple[int, int]] = defaultdict(lambda: (0, 0))
    for sid in sids:
        project = sid_to_project.get(sid)
        if not project:
            continue
        for d_offset in range((today - current_monday).days + 1 + week_count * 7):
            d = current_monday - timedelta(weeks=week_count) + timedelta(days=d_offset)
            key = (sid, d)
            if key in tokens_per_day:
                in_t, out_t = tokens_per_day[key]
                prev_in, prev_out = project_day_tokens[(project, d)]
                project_day_tokens[(project, d)] = (prev_in + in_t, prev_out + out_t)

    # Build weeks for each project
    result: dict[str, ProjectTimeSeries] = {}
    for project in set(sid_to_project.values()):
        weeks: list[ProjectTimeSeriesWeek] = []
        for i in range(week_count + 1):
            offset = week_count - i
            monday = current_monday - timedelta(weeks=offset)
            label = f"W-{monday.isocalendar()[1]}"
            days: list[ProjectTimeSeriesDay | None] = []
            for d_idx in range(7):
                day_date = monday + timedelta(days=d_idx)
                if day_date > today:
                    days.append(None)
                    continue
                in_t, out_t = project_day_tokens.get((project, day_date), (0, 0))
                if in_t == 0 and out_t == 0:
                    days.append(None)
                    continue
                days.append(ProjectTimeSeriesDay(
                    date=day_date.isoformat(),
                    input=in_t,
                    output=out_t,
                    total=in_t + out_t,
                    cost_usd=compute_cost(in_t, out_t, settings),
                ))
            weeks.append(ProjectTimeSeriesWeek(label=label, days=days))
        result[project] = ProjectTimeSeries(weeks=weeks)
    return result


# ---- Public entry --------------------------------------------------------


def build_snapshot(
    con: sqlite3.Connection, now: datetime
) -> ProjectsSnapshot:
    """Build the full /api/v1/projects/snapshot response."""
    today = now.date()
    start_ms = time_svc.start_of_window_ms(today, TZ, settings.week_count)
    end_ms = time_svc.end_of_now_ms(now)

    # 1. Sessions in window (for project identification + duration)
    sessions = _sessions_in_window(con, start_ms, end_ms)
    if not sessions:
        return ProjectsSnapshot(
            now_msk=now.isoformat(),
            window=ProjectsWindow(start="", end=today.isoformat()),
            projects=[],
        )

    # 2. Tokens per session
    tokens = _tokens_per_session(con, start_ms, end_ms)

    # 3. Session metadata (workspaceDir, status)
    sids = list(sessions.keys())
    meta = _sessions_meta(con, sids)

    # 4. Group by project
    grouped: dict[str, dict[str, Any]] = {}
    sid_to_project: dict[str, str] = {}
    for sid, (mn, mx, _user) in sessions.items():
        rec = meta.get(sid, {})
        wsd = rec.get("workspaceDir")
        workspace_dir = wsd if isinstance(wsd, str) else None
        status = rec.get("status")
        project = project_from_workspace(workspace_dir)
        if project is None:
            continue
        sid_to_project[sid] = project
        in_t, out_t = tokens.get(sid, (0, 0))
        duration_ms = mx - mn
        bucket = grouped.setdefault(project, {
            "max_ms": mx,
            "duration_ms": 0,
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "sessions": 0,
            "is_active": False,
        })
        if mx > bucket["max_ms"]:
            bucket["max_ms"] = mx
        bucket["duration_ms"] += duration_ms
        bucket["tokens"] += in_t + out_t
        bucket["input_tokens"] += in_t
        bucket["output_tokens"] += out_t
        bucket["sessions"] += 1
        if status == "started":
            bucket["is_active"] = True

    # 5. Time series per project
    tokens_per_day = _tokens_per_session_per_day(con, start_ms, end_ms)
    time_series_by_project = _build_time_series(sid_to_project, sids, tokens_per_day, today)

    # 6. Build rows sorted by max_ms desc, tie-break tokens desc
    rows_data: list[dict[str, Any]] = []
    for project, b in grouped.items():
        rows_data.append({
            "project": project,
            "max_ms": b["max_ms"],
            "duration_ms": b["duration_ms"],
            "tokens": b["tokens"],
            "input_tokens": b["input_tokens"],
            "output_tokens": b["output_tokens"],
            "sessions": b["sessions"],
            "is_active": b["is_active"],
            "has_ts": b["tokens"] > 0,  # has time series only if any tokens
        })
    rows_data.sort(key=lambda r: (r["max_ms"], r["tokens"]), reverse=True)

    # 7. Window dates
    start_monday, _ = time_svc.current_week_window(today, settings.week_count)
    window = ProjectsWindow(start=start_monday.isoformat(), end=today.isoformat())

    # 8. Assemble response
    projects: list[ProjectRow] = []
    for r in rows_data:
        in_t = r["input_tokens"]
        out_t = r["output_tokens"]
        ts = time_series_by_project.get(r["project"]) if r["has_ts"] else None
        projects.append(ProjectRow(
            project=r["project"],
            last_update=datetime.fromtimestamp(r["max_ms"] / 1000, tz=TZ).date().isoformat(),
            max_ms=r["max_ms"],
            duration_ms=r["duration_ms"],
            tokens=r["tokens"],
            input_tokens=in_t,
            output_tokens=out_t,
            cost_usd=compute_cost(in_t, out_t, settings),
            sessions=r["sessions"],
            is_active=r["is_active"],
            time_series=ts,
        ))

    return ProjectsSnapshot(
        now_msk=now.isoformat(),
        window=window,
        projects=projects,
    )
