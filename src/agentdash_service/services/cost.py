"""Cost computation. Pure function, no I/O."""

from __future__ import annotations

from ..config import Settings


def compute_cost(input_tokens: int, output_tokens: int, settings: Settings) -> float:
    """USD cost for the given token counts at configured prices.

    Prices are in USD per 1M tokens (OpenRouter convention).
    """
    in_cost = (input_tokens / 1_000_000) * settings.cost_input_per_1m_usd
    out_cost = (output_tokens / 1_000_000) * settings.cost_output_per_1m_usd
    return in_cost + out_cost
