"""Projects snapshot endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ...db import DbConnection
from ...models.projects import ProjectsSnapshot
from ...services import projects as svc
from ...services.time import now_in_tz

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects/snapshot", response_model=ProjectsSnapshot)
def projects_snapshot(con: DbConnection) -> ProjectsSnapshot:
    """Full data for `project-dashboard.html`. One request = one snapshot."""
    return svc.build_snapshot(con, now_in_tz())
