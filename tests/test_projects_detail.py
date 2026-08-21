"""Tests for /api/v1/projects/{slug}/detail."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agentdash_service.main import app

client = TestClient(app)


def test_project_detail_for_known_project() -> None:
    """The 'agent-tokens-dashboard' project should always have data in this workspace."""
    response = client.get("/api/v1/projects/agent-tokens-dashboard/detail")
    assert response.status_code == 200
    data = response.json()
    assert data["project"] == "agent-tokens-dashboard"
    assert "window" in data
    assert "start" in data["window"]
    assert "end" in data["window"]
    assert "days" in data
    assert "hours" in data
    assert "max_value" in data
    assert "totals_tokens" in data
    assert "active_dates" in data
    # 5 weeks x 7 days = 35 entries (some may be None)
    assert len(data["days"]) == 35
    # days entries are either null or a dict
    for d in data["days"]:
        if d is not None:
            assert "date" in d
            assert "total" in d
            assert "intensity" in d
            assert 0 <= d["intensity"] <= 4
    # hours: at least one date
    assert len(data["hours"]) >= 1
    first_date = next(iter(data["hours"]))
    assert len(data["hours"][first_date]) == 24
    for h in data["hours"][first_date]:
        assert 0 <= h["hour"] <= 23
        assert h["total"] >= 0


def test_project_detail_404_for_unknown_project() -> None:
    response = client.get("/api/v1/projects/this-project-does-not-exist-12345/detail")
    assert response.status_code == 404


def test_project_detail_with_pinned_now() -> None:
    """build_project_detail is deterministic given `now`."""
    from agentdash_service.db import get_db
    from agentdash_service.services.projects import build_project_detail

    with get_db() as con:
        now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        d = build_project_detail(con, "agent-tokens-dashboard", now)
    if d is None:
        pytest.skip("no data for this project on this date")
    assert d.now_msk == "2026-08-21T12:00:00+03:00"
    assert d.project == "agent-tokens-dashboard"
    assert d.window.end == "2026-08-21"
    assert len(d.days) == 35
