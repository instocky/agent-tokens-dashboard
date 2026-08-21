"""Smoke test for /api/v1/tokens/snapshot."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from agentdash_service.main import app
from agentdash_service.services.tokens import build_snapshot

client = TestClient(app)


def test_tokens_snapshot_returns_200() -> None:
    response = client.get("/api/v1/tokens/snapshot")
    assert response.status_code == 200
    data = response.json()
    assert "now_msk" in data
    assert "today" in data
    assert "now_session" in data
    assert "weekly" in data
    assert "sparklines" in data


def test_tokens_snapshot_shape() -> None:
    response = client.get("/api/v1/tokens/snapshot")
    data = response.json()

    # today
    today = data["today"]
    assert "date" in today
    assert "totals" in today
    assert "meta" in today
    assert len(today["hourly"]) == 24
    assert len(today["windows"]) == 5
    assert "current_window" in today
    assert "peak_hour" in today  # can be null

    # weekly
    weekly = data["weekly"]
    assert "weeks" in weekly
    assert "cap_tokens" in weekly
    assert "threshold_tokens" in weekly
    assert "weekly_spent_tokens" in weekly
    assert "days_left" in weekly
    # week_count (4 finished) + 1 current = 5 weeks
    assert len(weekly["weeks"]) == 5

    # sparklines
    assert "current" in data["sparklines"]
    assert "today" in data["sparklines"]
    assert "window" in data["sparklines"]


def test_build_snapshot_with_pinned_now() -> None:
    """build_snapshot is deterministic given `now` — no DB mutations involved."""
    # We pass a fixed datetime; the function reads from the real DB but
    # uses our `now` for window/derivation. This proves the function
    # signature works.
    from agentdash_service.db import get_db
    with get_db() as con:
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        snap = build_snapshot(con, now)
    assert snap.now_msk == "2026-08-21T12:00:00+03:00"
    assert snap.today.date == "2026-08-21"
    assert len(snap.today.hourly) == 24
    assert len(snap.today.windows) == 5
    assert len(snap.weekly.weeks) == 5
    assert snap.weekly.days_left >= 1
