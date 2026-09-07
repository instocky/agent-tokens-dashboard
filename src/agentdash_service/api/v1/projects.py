"""Projects snapshot endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...db import DbConnection
from ...models.projects import ProjectDetail, ProjectsActivitySnapshot, ProjectsSnapshot
from ...services import projects as svc
from ...services.time import now_in_tz

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects/snapshot", response_model=ProjectsSnapshot)
def projects_snapshot(con: DbConnection) -> ProjectsSnapshot:
    """Full data for `project-dashboard.html`. One request = one snapshot."""
    return svc.build_snapshot(con, now_in_tz())


@router.get("/projects/activity", response_model=ProjectsActivitySnapshot)
def projects_activity(
    con: DbConnection,
    month: str | None = None,
) -> ProjectsActivitySnapshot:
    """Project activity for a selected calendar month."""
    try:
        return svc.build_activity_snapshot(con, now_in_tz(), month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{slug}/detail", response_model=ProjectDetail)
def project_detail(slug: str, con: DbConnection) -> ProjectDetail:
    """Per-project detail: 5-week day grid + per-day 24h hours.

    Fetched on demand when a row in `project-dashboard.html` is expanded.
    404 if the project has no sessions in the window.
    """
    detail = svc.build_project_detail(con, slug, now_in_tz())
    if detail is None:
        raise HTTPException(status_code=404, detail=f"project '{slug}' has no sessions in window")
    return detail
