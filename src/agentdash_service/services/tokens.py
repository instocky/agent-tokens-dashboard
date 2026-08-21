"""SQL + assembly for /api/v1/tokens/snapshot.

All read-only. Combines raw SQL with derivations (cost, intensity,
sparklines, weekly aggregation) to produce the full snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from ..config import TZ, settings
from ..models.tokens import (
    CurrentWindowRef,
    HourlyBar,
    NowSession,
    PeakHour,
    Sparklines,
    TodayBlock,
    TodayMeta,
    TokensSnapshot,
    TokensSplit,
    WeeklyBlock,
    WeeklyDay,
    WeeklyWeek,
    WindowAgg,
)
from . import time as time_svc
from .cost import compute_cost
from .project import project_from_workspace

# SQLite needs the offset as a string literal in the date() function.
# TZ is fixed to MSK (UTC+3) for now. If AGENTDASH_TIMEZONE changes,
# this needs to be derived dynamically.
MSK_OFFSET = "+3 hours"


# ---- SQL helpers ---------------------------------------------------------


def _aggregate_by_hour_split(
    con: sqlite3.Connection, start_ts_ms: int
) -> dict[tuple[date, int], tuple[int, int]]:
    """Returns {(msk_date, msk_hour): (input_sum, output_sum)} for ts >= start_ts_ms."""
    sql = f"""
        SELECT date(ts / 1000, 'unixepoch', '{MSK_OFFSET}') AS msk_date,
               CAST(strftime('%H', ts / 1000, 'unixepoch', '{MSK_OFFSET}') AS INT) AS msk_hour,
               SUM(input_tokens) AS in_sum,
               SUM(output_tokens) AS out_sum
        FROM local_runtime_token_usage
        WHERE ts >= ?
        GROUP BY msk_date, msk_hour
    """
    out: dict[tuple[date, int], tuple[int, int]] = {}
    for date_str, hour, in_sum, out_sum in con.execute(sql, (start_ts_ms,)):
        out[(date.fromisoformat(date_str), int(hour))] = (int(in_sum or 0), int(out_sum or 0))
    return out


def _today_meta(
    con: sqlite3.Connection, since_ts_ms: int
) -> tuple[int, int, float]:
    """Returns (sessions, user_messages, avg_requests_per_session) for today."""
    sess_row = con.execute(
        "SELECT COUNT(DISTINCT session_id) FROM local_runtime_message_rows "
        "WHERE created_at_ms >= ?",
        (since_ts_ms,),
    ).fetchone()
    sessions = int(sess_row[0]) if sess_row else 0

    msg_row = con.execute(
        "SELECT COUNT(*) FROM local_runtime_message_rows "
        "WHERE role = 'user' AND created_at_ms >= ?",
        (since_ts_ms,),
    ).fetchone()
    user_messages = int(msg_row[0]) if msg_row else 0

    avg = (user_messages / sessions) if sessions > 0 else 0.0
    return sessions, user_messages, avg


def _session_record(con: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Read record_json for a session, return {} on any failure."""
    try:
        row = con.execute(
            "SELECT record_json FROM local_runtime_sessions WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or not row[0]:
        return {}
    try:
        rec = json.loads(row[0])
        return rec if isinstance(rec, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _current_session(
    con: sqlite3.Connection, since_ts_ms: int
) -> dict[str, Any] | None:
    """Returns session info dict or None if no activity today."""
    sid_row = con.execute(
        "SELECT session_id FROM local_runtime_message_rows "
        "WHERE created_at_ms >= ? "
        "ORDER BY created_at_ms DESC LIMIT 1",
        (since_ts_ms,),
    ).fetchone()
    if sid_row is None or sid_row[0] is None:
        return None
    session_id = str(sid_row[0])

    tok_row = con.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
        "FROM local_runtime_token_usage "
        "WHERE session_id = ? AND ts >= ?",
        (session_id, since_ts_ms),
    ).fetchone()
    input_t = int(tok_row[0]) if tok_row else 0
    output_t = int(tok_row[1]) if tok_row else 0

    req_row = con.execute(
        "SELECT COUNT(*) FROM local_runtime_message_rows "
        "WHERE session_id = ? AND role = 'user' AND created_at_ms >= ?",
        (session_id, since_ts_ms),
    ).fetchone()
    user_requests = int(req_row[0]) if req_row else 0

    rec = _session_record(con, session_id)
    path = rec.get("workspaceDir") if isinstance(rec.get("workspaceDir"), str) else None
    title = rec.get("title") if isinstance(rec.get("title"), str) else None

    dur_row = con.execute(
        "SELECT MIN(created_at_ms), MAX(created_at_ms) "
        "FROM local_runtime_message_rows "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    duration_ms: int | None = None
    if dur_row and dur_row[0] is not None and dur_row[1] is not None:
        duration_ms = int(dur_row[1]) - int(dur_row[0])

    return {
        "session_id": session_id,
        "input_tokens": input_t,
        "output_tokens": output_t,
        "user_requests": user_requests,
        "path": path,
        "project": project_from_workspace(path),
        "title": title,
        "duration_ms": duration_ms,
    }


# ---- Derivation helpers --------------------------------------------------


def _compute_intensity(bars: list[dict[str, Any]]) -> None:
    """Mutate `bars` in place: set 'intensity' field per quartile of non-zero values.

    L0 = empty. L1..L4 = quartile buckets. PEAK = the (single) max.
    """
    non_zero = [b["total"] for b in bars if b["total"] > 0]
    if not non_zero:
        for b in bars:
            b["intensity"] = "L0"
        return

    sorted_vals = sorted(non_zero)
    n = len(sorted_vals)
    max_val = sorted_vals[-1]

    if n == 1:
        # Single non-zero bar — it's the peak.
        for b in bars:
            b["intensity"] = "PEAK" if b["total"] == max_val else "L0"
        return

    q1 = sorted_vals[n // 4]
    q2 = sorted_vals[n // 2]
    q3 = sorted_vals[3 * n // 4]

    for b in bars:
        v = b["total"]
        if v == 0:
            b["intensity"] = "L0"
        elif v == max_val and max_val > 0:
            b["intensity"] = "PEAK"
        elif v <= q1:
            b["intensity"] = "L1"
        elif v <= q2:
            b["intensity"] = "L2"
        elif v <= q3:
            b["intensity"] = "L3"
        else:
            b["intensity"] = "L4"


def _current_window_for(hour: int) -> CurrentWindowRef:
    for w in time_svc.WINDOWS:
        if hour in w["hours"]:
            return CurrentWindowRef(name=w["name"], label=w["label"])
    return CurrentWindowRef(name="unknown", label="—")


def _weekly_threshold(cap: int, spent: int, days_left: int) -> int | None:
    if days_left <= 0:
        return None
    remaining = cap - spent
    if remaining <= 0:
        return 0
    return remaining // days_left


# ---- Block builders ------------------------------------------------------


def _build_today_block(
    today: date,
    now: datetime,
    hourly_map: dict[tuple[date, int], tuple[int, int]],
    sessions: int,
    user_messages: int,
    avg: float,
) -> TodayBlock:
    current_hour = now.hour

    # 24 hourly bars
    raw_bars: list[dict[str, Any]] = []
    for h in range(24):
        in_t, out_t = hourly_map.get((today, h), (0, 0))
        total = in_t + out_t
        raw_bars.append({
            "hour": h,
            "input": in_t,
            "output": out_t,
            "total": total,
            "cost_usd": compute_cost(in_t, out_t, settings),
            "is_current": h == current_hour,
            "is_future": h > current_hour,
            "is_empty": total == 0,
        })
    _compute_intensity(raw_bars)
    hourly = [HourlyBar(**b) for b in raw_bars]

    # Totals
    total_in = sum(b.input for b in hourly)
    total_out = sum(b.output for b in hourly)
    totals = TokensSplit(
        input=total_in,
        output=total_out,
        total=total_in + total_out,
        cost_usd=compute_cost(total_in, total_out, settings),
    )

    # 5 windows today
    windows: list[WindowAgg] = []
    for w in time_svc.WINDOWS:
        w_in = sum(hourly_map.get((today, h), (0, 0))[0] for h in w["hours"])
        w_out = sum(hourly_map.get((today, h), (0, 0))[1] for h in w["hours"])
        windows.append(WindowAgg(
            name=w["name"],
            label=w["label"],
            input=w_in,
            output=w_out,
            total=w_in + w_out,
            cost_usd=compute_cost(w_in, w_out, settings),
            is_current=current_hour in w["hours"],
        ))

    # Peak hour (max non-zero today)
    peak_hour: PeakHour | None = None
    non_empty = [b for b in hourly if b.total > 0]
    if non_empty:
        peak = max(non_empty, key=lambda b: b.total)
        peak_hour = PeakHour(hour=peak.hour, tokens=peak.total, cost_usd=peak.cost_usd)

    return TodayBlock(
        date=today.isoformat(),
        totals=totals,
        meta=TodayMeta(
            sessions=sessions,
            user_messages=user_messages,
            avg_requests_per_session=avg,
        ),
        hourly=hourly,
        windows=windows,
        current_window=_current_window_for(current_hour),
        peak_hour=peak_hour,
    )


def _build_now_session(s: dict[str, Any] | None) -> NowSession | None:
    if s is None:
        return None
    in_t = s["input_tokens"]
    out_t = s["output_tokens"]
    return NowSession(
        session_id=s["session_id"],
        tokens=TokensSplit(
            input=in_t,
            output=out_t,
            total=in_t + out_t,
            cost_usd=compute_cost(in_t, out_t, settings),
        ),
        user_requests=s["user_requests"],
        path=s["path"],
        project=s["project"],
        title=s["title"],
        duration_ms=s["duration_ms"],
    )


def _build_weekly_block(
    today: date,
    now: datetime,
    hourly_map: dict[tuple[date, int], tuple[int, int]],
) -> WeeklyBlock:
    iso = today.isocalendar()
    current_monday = today - timedelta(days=iso[2] - 1)
    week_count = settings.week_count

    weeks: list[WeeklyWeek] = []
    weekly_spent = 0
    for i in range(week_count + 1):  # 0..week_count inclusive = week_count+1 weeks total
        # weeks[0] = oldest, weeks[-1] = current
        offset = week_count - i
        monday = current_monday - timedelta(weeks=offset)
        is_current = monday == current_monday
        label = f"W-{monday.isocalendar()[1]}"

        days: list[WeeklyDay | None] = []
        week_in = 0
        week_out = 0
        for d_idx in range(7):
            day_date = monday + timedelta(days=d_idx)
            if day_date > today:
                # Future day in any week — null
                days.append(None)
                continue
            # Past or today — sum hourly
            in_sum = sum(hourly_map.get((day_date, h), (0, 0))[0] for h in range(24))
            out_sum = sum(hourly_map.get((day_date, h), (0, 0))[1] for h in range(24))
            if in_sum == 0 and out_sum == 0:
                days.append(None)
                continue
            day_cost = compute_cost(in_sum, out_sum, settings)
            days.append(WeeklyDay(
                date=day_date.isoformat(),
                input=in_sum,
                output=out_sum,
                total=in_sum + out_sum,
                cost_usd=day_cost,
            ))
            if is_current:
                week_in += in_sum
                week_out += out_sum

        if is_current:
            weekly_spent = week_in + week_out

        weeks.append(WeeklyWeek(
            label=label,
            monday=monday.isoformat(),
            is_current=is_current,
            days=days,
        ))

    days_left = time_svc.days_left_in_week(today)
    threshold = _weekly_threshold(settings.weekly_cap_tokens, weekly_spent, days_left)

    return WeeklyBlock(
        weeks=weeks,
        cap_tokens=settings.weekly_cap_tokens,
        threshold_tokens=threshold,
        weekly_spent_tokens=weekly_spent,
        days_left=days_left,
    )


def _build_sparklines(
    today: date,
    now: datetime,
    hourly_map: dict[tuple[date, int], tuple[int, int]],
) -> Sparklines:
    current_hour = now.hour

    # today: full 24 hours
    today_points = [
        sum(hourly_map.get((today, h), (0, 0))) for h in range(24)
    ]

    # current: trailing 3 hours ending at current hour
    trailing = [current_hour - 2, current_hour - 1, current_hour]
    current_points = [
        sum(hourly_map.get((today, h), (0, 0))) for h in trailing
    ]

    # window: current 5h window
    cw = _current_window_for(current_hour)
    window_hours = next(
        (w["hours"] for w in time_svc.WINDOWS if w["name"] == cw.name),
        [current_hour],
    )
    window_points = [
        sum(hourly_map.get((today, h), (0, 0))) for h in window_hours
    ]

    return Sparklines(
        current=current_points,
        today=today_points,
        window=window_points,
    )


# ---- Public entry --------------------------------------------------------


def build_snapshot(con: sqlite3.Connection, now: datetime) -> TokensSnapshot:
    """Build the full /api/v1/tokens/snapshot response."""
    today = now.date()
    start_ms = time_svc.start_of_window_ms(today, TZ, settings.week_count)
    since_today_ms = time_svc.since_midnight_ms(today, TZ)

    hourly_map = _aggregate_by_hour_split(con, start_ms)
    sessions, user_messages, avg = _today_meta(con, since_today_ms)
    now_session = _current_session(con, since_today_ms)

    return TokensSnapshot(
        now_msk=now.isoformat(),
        today=_build_today_block(today, now, hourly_map, sessions, user_messages, avg),
        now_session=_build_now_session(now_session),
        weekly=_build_weekly_block(today, now, hourly_map),
        sparklines=_build_sparklines(today, now, hourly_map),
    )
