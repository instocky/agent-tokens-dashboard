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


class ProjectActivityDay(BaseModel):
    date: str
    active: bool
    future: bool


class ProjectActivityRow(BaseModel):
    project: str
    days: list[ProjectActivityDay]
    duration_ms: int


class ProjectsActivitySnapshot(BaseModel):
    now_msk: str
    month: str
    months: list[str]
    projects: list[ProjectActivityRow]


# ----- /api/v1/projects/{slug}/detail --------------------------------------


class ProjectHourCell(BaseModel):
    hour: int  # 0..23
    total: int
    input: int
    output: int


class ProjectDetailDay(BaseModel):
    date: str
    input: int
    output: int
    total: int
    cost_usd: float
    # GitHub-style intensity 0..4 (0 = empty/pale, 4 = max)
    intensity: int


class ProjectDetailWindow(BaseModel):
    start: str
    end: str


class ProjectDetail(BaseModel):
    now_msk: str
    project: str
    window: ProjectDetailWindow
    # 5 weeks x 7 days, oldest first. None for out-of-window or no-data days.
    days: list[ProjectDetailDay | None]
    # date (YYYY-MM-DD) -> 24 hour cells (oldest hour first).
    hours: dict[str, list[ProjectHourCell]]
    # Meta for the day grid scaling: max value in the window (for intensity calc).
    max_value: int
    # Total tokens / cost in the window (for header summary).
    totals_input: int
    totals_output: int
    totals_tokens: int
    totals_cost_usd: float
    # Set of dates with at least one token (for "selected day" default).
    active_dates: list[str]
