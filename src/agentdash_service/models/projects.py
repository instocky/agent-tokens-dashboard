"""Pydantic DTOs for /api/v1/projects/snapshot response.

Shape matches docs/API_CONTRACT.md.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProjectsWindow(BaseModel):
    start: str  # ISO date
    end: str    # ISO date


class ProjectTimeSeriesDay(BaseModel):
    date: str
    input: int
    output: int
    total: int
    cost_usd: float


class ProjectTimeSeriesWeek(BaseModel):
    label: str
    days: list[ProjectTimeSeriesDay | None]


class ProjectTimeSeries(BaseModel):
    weeks: list[ProjectTimeSeriesWeek]


class ProjectRow(BaseModel):
    project: str
    last_update: str
    max_ms: int
    duration_ms: int
    tokens: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    sessions: int
    is_active: bool
    time_series: ProjectTimeSeries | None


class ProjectsSnapshot(BaseModel):
    now_msk: str
    window: ProjectsWindow
    projects: list[ProjectRow]
