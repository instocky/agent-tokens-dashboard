"""Application settings — single source of truth for env-driven config."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven config. Read once at module import.

    Env prefix is `AGENTDASH_` (e.g. `AGENTDASH_PORT=8021`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AGENTDASH_",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = "127.0.0.1"
    port: int = 8021
    log_level: str = "INFO"

    # Database (read-only)
    db_path: Path = Path("C:/Users/user/.minimax/v2/sqlite/runtime-state.sqlite")

    # Time
    timezone: str = "Europe/Moscow"

    # Cost (per 1M tokens, USD)
    default_model: str = "minimax-m3"
    cost_input_per_1m_usd: float = 0.23
    cost_output_per_1m_usd: float = 0.96
    cost_cache_read_per_1m_usd: float = 0.06

    # Weekly quota
    weekly_cap_tokens: int = 60_000_000

    # Window
    week_count: int = 4


settings = Settings()
TZ: ZoneInfo = ZoneInfo(settings.timezone)
