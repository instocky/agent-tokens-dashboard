"""Smoke tests for /api/v1/health and /api/v1/ready.

These are the first endpoints implemented in Phase 1. They prove the
FastAPI app boots, the Settings load, the read-only SQLite helper works,
and the readiness check actually pings the DB.
"""

from fastapi.testclient import TestClient

from agentdash_service.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_ready_when_db_reachable() -> None:
    response = client.get("/api/v1/ready")
    # DB is at the default path (C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite)
    # — should be reachable on this machine.
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_route_registered() -> None:
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/v1/health" in paths
    assert "/api/v1/ready" in paths
