"""Fail-closed market-mode selection with independent readiness gates."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    GridMode,
    InventorySnapshot,
    MarketState,
    ModeDecision,
    OptionsSnapshot,
    RiskGateState,
)


@dataclass(frozen=True, slots=True)
class ModeConfig:
    max_option_age_seconds: float = 15.0
    future_tolerance_seconds: float = 2.0


DEFAULT_MODE_CONFIG = ModeConfig()


def determine_mode(
    *,
    state: MarketState,
    perp_market_ready: bool,
    options: OptionsSnapshot,
    inventory: InventorySnapshot,
    risk: RiskGateState,
    decision_timestamp: float,
    config: ModeConfig | None = None,
) -> ModeDecision:
    config = DEFAULT_MODE_CONFIG if config is None else config
    reasons: list[str] = []
    options_ready = options.option_data_available
    if not perp_market_ready:
        reasons.append("perpetual_market_not_ready")
    if not options_ready:
        reasons.append("options_data_unavailable")
    option_age = (
        None if options.source_timestamp is None else decision_timestamp - options.source_timestamp
    )
    if (
        option_age is None
        or option_age < -config.future_tolerance_seconds
        or option_age > config.max_option_age_seconds
    ):
        reasons.append("options_data_stale_or_missing")
    if options.received_timestamp is None:
        reasons.append("options_receipt_timestamp_missing")
    elif options.received_timestamp > decision_timestamp + config.future_tolerance_seconds:
        reasons.append("options_receipt_timestamp_in_future")
    if options.source_timestamp is not None and options.received_timestamp is not None:
        if options.source_timestamp > options.received_timestamp + config.future_tolerance_seconds:
            reasons.append("options_source_after_receipt")
    if options.environment != "mainnet":
        reasons.append("options_environment_not_mainnet")
    if options.underlying != "SOL":
        reasons.append("options_underlying_not_SOL")
    if state == MarketState.INITIALIZING:
        reasons.append("iv_state_warming_up")
    if inventory.hard_limit_reached:
        reasons.append("hard_inventory_limit")
    # A disarmed/shadow controller still needs to classify the market and
    # publish its intended grid. Execution gates are deliberately not copied
    # into classification reasons: ``iv_ratio_normal`` and the selected mode
    # remain informational even when live execution is blocked.
    if state == MarketState.LOW:
        mode = GridMode.AGGRESSIVE
        reasons.append("low_iv_aggressive_grid")
    elif state in {MarketState.HIGH, MarketState.EXTREME}:
        mode = GridMode.DEFENSIVE
        reasons.append("elevated_iv_defensive_grid")
    else:
        mode = GridMode.NORMAL
        reasons.append("normal_iv_grid")

    # Inventory limits are the one mode-independent reason to suppress a
    # side. Data/risk readiness remains visible in the reasons and gates.
    buy_allowed = not inventory.long_soft_limit and not inventory.hard_limit_reached
    sell_allowed = not inventory.short_soft_limit and not inventory.hard_limit_reached
    if not buy_allowed:
        reasons.append(
            "hard_inventory_limit_suppresses_buys"
            if inventory.hard_limit_reached
            else "long_inventory_soft_limit_suppresses_buys"
        )
    if not sell_allowed:
        reasons.append(
            "hard_inventory_limit_suppresses_sells"
            if inventory.hard_limit_reached
            else "short_inventory_soft_limit_suppresses_sells"
        )
    return ModeDecision(
        mode=mode,
        state=state,
        perp_market_ready=perp_market_ready,
        options_data_available=options_ready,
        buy_allowed=buy_allowed,
        sell_allowed=sell_allowed,
        decision_timestamp=decision_timestamp,
        reasons=tuple(dict.fromkeys(reasons)),
    )
