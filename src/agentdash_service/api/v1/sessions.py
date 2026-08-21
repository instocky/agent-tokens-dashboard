"""Sessions snapshot endpoint."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ...db import db_session
from ...models.sessions import SessionsSnapshot
from ...services import sessions as svc
from ...services.time import now_in_tz

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/sessions/snapshot", response_model=SessionsSnapshot)
def sessions_snapshot(
    con: sqlite3.Connection = Depends(db_session),
) -> SessionsSnapshot:
    """Full data for `session-dashboard.html`. One request = one snapshot."""
    return svc.build_snapshot(con, now_in_tz())
