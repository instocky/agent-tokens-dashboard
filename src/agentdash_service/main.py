"""FastAPI app — health and readiness endpoints.

Snapshot endpoints (tokens / projects / sessions) land in Phase 4.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .api.v1 import projects as projects_router
from .api.v1 import tokens as tokens_router
from .config import settings
from .db import get_db

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(
    title="agentdash-service",
    version=__version__,
    description="Read-only HTTP API for token-usage dashboards",
)

app.include_router(tokens_router.router)
app.include_router(projects_router.router)


@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    """Liveness. Always 200 if process is up."""
    return {"status": "ok"}


@app.get("/api/v1/ready")
async def ready() -> JSONResponse:
    """Readiness. Pings DB. 200 if OK, 503 if not."""
    try:
        with get_db() as con:
            con.execute("SELECT 1").fetchone()
    except Exception as e:  # noqa: BLE001 — readiness probe, log and report
        log.warning("readiness check failed", exc_info=True)
        payload: dict[str, Any] = {"status": "not-ready", "reason": str(e)}
        return JSONResponse(status_code=503, content=payload)
    return JSONResponse(status_code=200, content={"status": "ready"})
