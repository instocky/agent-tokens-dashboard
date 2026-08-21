"""Tokens snapshot endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ...db import DbConnection
from ...models.tokens import TokensSnapshot
from ...services import tokens as svc
from ...services.time import now_in_tz

router = APIRouter(prefix="/api/v1", tags=["tokens"])


@router.get("/tokens/snapshot", response_model=TokensSnapshot)
def tokens_snapshot(con: DbConnection) -> TokensSnapshot:
    """Full data for `dashboard.html`. One request = one snapshot."""
    return svc.build_snapshot(con, now_in_tz())
