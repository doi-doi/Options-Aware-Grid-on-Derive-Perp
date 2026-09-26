"""Research-only grid-width policies."""

from __future__ import annotations

import math
from enum import StrEnum

from ..models import MarketState

YEAR_SECONDS = 365.0 * 24.0 * 60.0 * 60.0


class WidthPolicy(StrEnum):
    STATIC_REGIME = "STATIC_REGIME"
    IV_SCALED = "IV_SCALED"


def clamp(value: float, lower: float, upper: float) -> float:
    if not all(math.isfinite(item) for item in (value, lower, upper)):
        raise ValueError("width values must be finite")
    if lower < 0 or upper < lower:
        raise ValueError("width bounds must satisfy 0 <= lower <= upper")
    return min(upper, max(lower, value))


def expected_move_pct(
    atm_iv: float, width_horizon_seconds: float, *, year_seconds: float = YEAR_SECONDS
) -> float:
    """Return the annualized-IV one-sigma move over the requested horizon."""

    if atm_iv <= 0 or width_horizon_seconds <= 0 or year_seconds <= 0:
        raise ValueError("IV and width horizon must be positive")
    return atm_iv * math.sqrt(width_horizon_seconds / year_seconds)


def iv_scaled_half_width(
    atm_iv: float,
    *,
    width_horizon_seconds: float,
    width_sigma_multiplier: float,
    min_half_width_pct: float,
    max_half_width_pct: float,
) -> float:
    if width_sigma_multiplier <= 0:
        raise ValueError("width_sigma_multiplier must be positive")
    expected = expected_move_pct(atm_iv, width_horizon_seconds)
    return clamp(
        width_sigma_multiplier * expected,
        min_half_width_pct,
        max_half_width_pct,
    )


def static_half_width(
    state: MarketState | str,
    *,
    aggressive_half_width_pct: float = 0.0075,
    normal_half_width_pct: float,
    defensive_half_width_pct: float,
) -> float:
    normalized = MarketState(state)
    if normalized == MarketState.LOW:
        return aggressive_half_width_pct
    if normalized == MarketState.NORMAL:
        return normal_half_width_pct
    if normalized in {MarketState.HIGH, MarketState.EXTREME}:
        return defensive_half_width_pct
    return 0.0


def research_half_width(
    policy: WidthPolicy | str,
    state: MarketState | str,
    *,
    atm_iv: float,
    aggressive_half_width_pct: float = 0.0075,
    normal_half_width_pct: float,
    defensive_half_width_pct: float,
    width_horizon_seconds: float,
    width_sigma_multiplier: float,
    min_half_width_pct: float,
    max_half_width_pct: float,
) -> tuple[float, float | None]:
    """Return ``(half_width, expected_move)`` without changing live geometry."""

    policy = WidthPolicy(policy)
    state = MarketState(state)
    if state == MarketState.INITIALIZING:
        return 0.0, None
    if policy == WidthPolicy.STATIC_REGIME:
        return static_half_width(
            state,
            aggressive_half_width_pct=aggressive_half_width_pct,
            normal_half_width_pct=normal_half_width_pct,
            defensive_half_width_pct=defensive_half_width_pct,
        ), None
    expected = expected_move_pct(atm_iv, width_horizon_seconds)
    return (
        clamp(
            width_sigma_multiplier * expected,
            min_half_width_pct,
            max_half_width_pct,
        ),
        expected,
    )
