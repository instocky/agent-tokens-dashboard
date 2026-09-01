"""Pydantic DTOs for /api/v1/tokens/snapshot response.

Shape matches docs/API_CONTRACT.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intensity = Literal["L0", "L1", "L2", "L3", "L4", "PEAK"]
WindowName = Literal["morning", "midday", "afternoon", "evening", "night"]


class TokensSplit(BaseModel):
    input: int
    output: int
    total: int
    cost_usd: float
    cache_read: int = 0
    cache_write: int = 0


class TodayMeta(BaseModel):
    sessions: int
    user_messages: int
    avg_requests_per_session: float


class HourlyBar(BaseModel):
    hour: int = Field(ge=0, le=23)
    input: int
    output: int
    total: int
    cost_usd: float
    intensity: Intensity
    is_current: bool
    is_future: bool
    is_empty: bool
    # Per-hour cache_read; summed at the TodayBlock / day level into
    # `totals.cache_read`. Kept separate from `total` so the
    # `total == input + output` invariant is preserved (see ADR-001).
    cache_read: int = 0


class WindowAgg(BaseModel):
    name: WindowName
    label: str
    input: int
    output: int
    total: int
    cost_usd: float
    is_current: bool


class CurrentWindowRef(BaseModel):
    name: str
    label: str


class PeakHour(BaseModel):
    hour: int
    tokens: int
    cost_usd: float


class TodayBlock(BaseModel):
    date: str
    totals: TokensSplit
    meta: TodayMeta
    hourly: list[HourlyBar]
    windows: list[WindowAgg]
    current_window: CurrentWindowRef
    peak_hour: PeakHour | None


class NowSession(BaseModel):
    session_id: str
    tokens: TokensSplit
    user_requests: int
    path: str | None
    project: str | None
    title: str | None
    duration_ms: int | None


class WeeklyDay(BaseModel):
    date: str
    input: int
    output: int
    total: int
    cost_usd: float
    cache_read: int = 0
    cache_cost_usd: float = 0.0


class WeeklyWeek(BaseModel):
    label: str
    monday: str
    is_current: bool
    days: list[WeeklyDay | None]  # None for future / no-data days
    cache_cost_usd: float = 0.0


class WeeklyBlock(BaseModel):
    weeks: list[WeeklyWeek]
    cap_tokens: int
    threshold_tokens: int | None
    weekly_spent_tokens: int
    days_left: int


class Sparklines(BaseModel):
    current: list[int]
    today: list[int]
    window: list[int]


class TokensSnapshot(BaseModel):
    now_msk: str
    today: TodayBlock
    now_session: NowSession | None
    weekly: WeeklyBlock
    sparklines: Sparklines
    # Per-day 24-hour bars for every date in the rolling weekly window.
    # Used by the dashboard for the WEEKLY → 24H STREAM drilldown
    # (see docs/ADR-002-selectable-stream-day.md). Keys are ISO dates
    # ("YYYY-MM-DD"); values are 24-element lists in the same shape as
    # `today.hourly`. Future days within the window are included as
    # zero-bars with `is_future=true`.
    hourly_by_date: dict[str, list[HourlyBar]]
