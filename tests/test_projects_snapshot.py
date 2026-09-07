"""Smoke test for /api/v1/projects/snapshot."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from agentdash_service.main import app
from agentdash_service.services.projects import build_snapshot

client = TestClient(app)


def test_projects_snapshot_returns_200() -> None:
    response = client.get("/api/v1/projects/snapshot")
    assert response.status_code == 200
    data = response.json()
    assert "now_msk" in data
    assert "window" in data
    assert "projects" in data
    assert isinstance(data["projects"], list)


def test_projects_snapshot_shape() -> None:
    response = client.get("/api/v1/projects/snapshot")
    data = response.json()
    if data["projects"]:
        p = data["projects"][0]
        assert "project" in p
        assert "last_update" in p
        assert "max_ms" in p
        assert "duration_ms" in p
        assert "tokens" in p
        assert "input_tokens" in p
        assert "output_tokens" in p
        assert "cost_usd" in p
        assert "sessions" in p
        assert "is_active" in p
        assert "time_series" in p
        # window
        assert "start" in data["window"]
        assert "end" in data["window"]


def test_projects_build_with_pinned_now() -> None:
    """build_snapshot is deterministic given `now`."""
    from agentdash_service.db import get_db
    with get_db() as con:
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        snap = build_snapshot(con, now)
    assert snap.now_msk == "2026-08-21T12:00:00+03:00"
    assert snap.window.end == "2026-08-21"
    assert isinstance(snap.projects, list)


def test_projects_activity_uses_calendar_months() -> None:
    from agentdash_service.db import get_db
    from agentdash_service.services.projects import build_activity_snapshot

    with get_db() as con:
        snap = build_activity_snapshot(
            con,
            datetime(2026, 9, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        )
    assert snap.month == "2026-09"
    assert len(snap.months) == 12
    assert all(len(project.days) == 30 for project in snap.projects)
