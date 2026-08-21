"""Pydantic DTOs for /api/v1/sessions/snapshot response.

Shape matches docs/API_CONTRACT.md.
"""

from __future__ import annotations

from pydantic import BaseModel


class SessionsWindow(BaseModel):
    start: str
    end: str


class SessionRow(BaseModel):
    session_id: str
    title: str | None
    project: str | None
    workspace_dir: str | None
    start_msk: str
    end_msk: str
    max_ms: int
    duration_ms: int
    tokens: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    requests: int
    is_active: bool


class SessionsSnapshot(BaseModel):
    now_msk: str
    window: SessionsWindow
    sessions: list[SessionRow]
