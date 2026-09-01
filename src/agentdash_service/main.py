"""FastAPI app — health and readiness endpoints.

Snapshot endpoints (tokens / projects / sessions) land in Phase 4.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .api.v1 import projects as projects_router
from .api.v1 import sessions as sessions_router
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

# Local-only CORS so the HTML dashboards (opened from file://) can
# fetch the JSON snapshots. Single user — open is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(tokens_router.router)
app.include_router(projects_router.router)
app.include_router(sessions_router.router)


@app.middleware("http")
async def force_json_charset(request, call_next):
    """Ensure application/json responses declare charset=utf-8.

    FastAPI's default JSONResponse emits Content-Type: application/json
    (no charset). Some downstream clients (notably Rainmeter's WebParser)
    decode as latin-1, producing '???' for any non-ASCII text. This
    middleware appends `; charset=utf-8` so clients honour UTF-8.
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json") and "charset" not in ctype.lower():
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


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
