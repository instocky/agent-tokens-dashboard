"""Sessions snapshot endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ...db import get_db
from ...models.sessions import SessionsSnapshot
from ...services import sessions as svc
from ...services.time import now_in_tz

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/sessions/snapshot", response_model=SessionsSnapshot)
def sessions_snapshot() -> SessionsSnapshot:
    """Full data for `session-dashboard.html`. One request = one snapshot."""
    with get_db() as con:
        return svc.build_snapshot(con, now_in_tz())
