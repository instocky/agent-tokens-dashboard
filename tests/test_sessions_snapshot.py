"""Smoke test for /api/v1/sessions/snapshot."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from agentdash_service.main import app
from agentdash_service.services.sessions import build_snapshot

client = TestClient(app)


def test_sessions_snapshot_returns_200() -> None:
    response = client.get("/api/v1/sessions/snapshot")
    assert response.status_code == 200
    data = response.json()
    assert "now_msk" in data
    assert "window" in data
    assert "sessions" in data
    assert isinstance(data["sessions"], list)


def test_sessions_snapshot_shape() -> None:
    response = client.get("/api/v1/sessions/snapshot")
    data = response.json()
    if data["sessions"]:
        s = data["sessions"][0]
        assert "session_id" in s
        assert "title" in s
        assert "project" in s
        assert "workspace_dir" in s
        assert "start_msk" in s
        assert "end_msk" in s
        assert "max_ms" in s
        assert "duration_ms" in s
        assert "tokens" in s
        assert "input_tokens" in s
        assert "output_tokens" in s
        assert "cost_usd" in s
        assert "requests" in s
        assert "is_active" in s


def test_sessions_build_with_pinned_now() -> None:
    """build_snapshot is deterministic given `now`."""
    from agentdash_service.db import get_db
    with get_db() as con:
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        snap = build_snapshot(con, now)
    assert snap.now_msk == "2026-08-21T12:00:00+03:00"
    assert snap.window.end == "2026-08-21"
    assert isinstance(snap.sessions, list)
