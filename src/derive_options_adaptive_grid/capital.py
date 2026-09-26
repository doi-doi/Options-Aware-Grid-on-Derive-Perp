"""Pure Decimal capital allocation for the Derive adaptive grid.

The controller supplies only the currently available USDC balance and static
policy caps.  This module has no Hummingbot dependency so the allocation
contract can be tested independently of an exchange runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .models import CapitalSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    """Static allocation policy; quote amounts are absolute USDC caps."""

    total_amount_quote: Decimal = Decimal("100")
    aggressive_total_quote: Decimal = Decimal("100")
    defensive_total_quote: Decimal = Decimal("60")
    max_position_quote: Decimal = Decimal("50")
    hard_position_quote: Decimal = Decimal("75")
    max_order_notional_quote: Decimal = Decimal("100")
    reserve_pct: Decimal = Decimal("0.20")
    reserve_quote: Decimal = Decimal("20")
    utilization_pct: Decimal = Decimal("0.50")
    fee_buffer_pct: Decimal = Decimal("0.02")
    fee_buffer_quote: Decimal = Decimal("5")
    hard_position_multiplier: Decimal = Decimal("1.25")


def _invalid_policy_errors(policy: CapitalPolicy) -> list[str]:
    errors: list[str] = []
    positive_caps = {
        "total_amount_quote": policy.total_amount_quote,
        "aggressive_total_quote": policy.aggressive_total_quote,
        "defensive_total_quote": policy.defensive_total_quote,
        "max_position_quote": policy.max_position_quote,
        "hard_position_quote": policy.hard_position_quote,
        "max_order_notional_quote": policy.max_order_notional_quote,
    }
    errors.extend(name for name, value in positive_caps.items() if value <= ZERO)
    if not ZERO <= policy.reserve_pct <= ONE:
        errors.append("reserve_pct_out_of_range")
    if policy.reserve_quote < ZERO:
        errors.append("reserve_quote_negative")
    if not ZERO < policy.utilization_pct <= ONE:
        errors.append("utilization_pct_out_of_range")
    if not ZERO <= policy.fee_buffer_pct:
        errors.append("fee_buffer_pct_negative")
    if policy.fee_buffer_quote < ZERO:
        errors.append("fee_buffer_quote_negative")
    if policy.hard_position_multiplier < ONE:
        errors.append("hard_position_multiplier_below_one")
    if policy.aggressive_total_quote > policy.total_amount_quote:
        errors.append("aggressive_cap_exceeds_total_cap")
    if policy.defensive_total_quote > policy.total_amount_quote:
        errors.append("defensive_cap_exceeds_total_cap")
    if policy.max_order_notional_quote > policy.total_amount_quote:
        errors.append("max_order_cap_exceeds_total_cap")
    if policy.hard_position_quote < policy.max_position_quote:
        errors.append("hard_position_cap_below_soft_cap")
    return errors


def calculate_capital_snapshot(
    available_collateral_quote: Decimal | int | float | str | None,
    policy: CapitalPolicy | None = None,
    *,
    source_timestamp: float | None = None,
    decision_timestamp: float | None = None,
    selected_leg_total_quote: Decimal | int | float | str = ZERO,
    selected_native_minimum_quote: Decimal | int | float | str = ZERO,
) -> CapitalSnapshot:
    """Return a deterministic allocation snapshot and fail closed on errors."""

    policy = CapitalPolicy() if policy is None else policy
    errors = _invalid_policy_errors(policy)
    available = _decimal(available_collateral_quote)
    selected_total = _decimal(selected_leg_total_quote)
    selected_minimum = _decimal(selected_native_minimum_quote)
    if available is None:
        errors.append("available_USDC_collateral_unavailable")
    elif available < ZERO:
        errors.append("available_USDC_collateral_negative")
    if selected_total is None or selected_total < ZERO:
        errors.append("selected_leg_total_invalid")
        selected_total = ZERO
    if selected_minimum is None or selected_minimum < ZERO:
        errors.append("selected_native_minimum_invalid")
        selected_minimum = ZERO

    if errors or available is None:
        return CapitalSnapshot(
            available_collateral_quote=available,
            selected_leg_total_quote=selected_total,
            selected_native_minimum_quote=selected_minimum,
            source_timestamp=source_timestamp,
            decision_timestamp=decision_timestamp,
            errors=tuple(dict.fromkeys(errors)),
        )

    reserve = max(policy.reserve_quote, available * policy.reserve_pct)
    deployable = max(ZERO, available - reserve)
    target = min(policy.total_amount_quote, deployable * policy.utilization_pct)
    normal = target
    aggressive = min(policy.aggressive_total_quote, target)
    defensive_ratio = policy.defensive_total_quote / policy.total_amount_quote
    defensive = min(policy.defensive_total_quote, target * defensive_ratio)
    soft = min(policy.max_position_quote, target)
    hard = min(
        policy.hard_position_quote,
        deployable,
        max(soft, soft * policy.hard_position_multiplier),
    )
    fee_buffer = max(policy.fee_buffer_quote, target * policy.fee_buffer_pct)

    if aggressive > normal:
        errors.append("aggressive_budget_exceeds_normal")
    if hard < soft:
        errors.append("dynamic_hard_cap_below_dynamic_soft_cap")
    if target <= ZERO:
        errors.append("target_strategy_allocation_zero")
    if fee_buffer > deployable:
        errors.append("fee_buffer_exceeds_deployable_collateral")
    if selected_total + fee_buffer > deployable:
        errors.append("selected_leg_plus_fee_buffer_exceeds_deployable_collateral")
    if selected_minimum + fee_buffer > deployable:
        errors.append("native_minimum_plus_fee_buffer_exceeds_deployable_collateral")

    return CapitalSnapshot(
        available_collateral_quote=available,
        reserve_quote=reserve,
        deployable_quote=deployable,
        target_strategy_quote=target,
        aggressive_budget_quote=aggressive,
        normal_budget_quote=normal,
        defensive_budget_quote=defensive,
        dynamic_soft_position_quote=soft,
        dynamic_hard_position_quote=hard,
        fee_buffer_quote=fee_buffer,
        selected_leg_total_quote=selected_total,
        selected_native_minimum_quote=selected_minimum,
        selected_leg_fundable=(
            selected_total + fee_buffer <= deployable
            and selected_minimum + fee_buffer <= deployable
        ),
        capital_ready=not errors,
        source_timestamp=source_timestamp,
        decision_timestamp=decision_timestamp,
        errors=tuple(dict.fromkeys(errors)),
    )


def with_selected_leg(
    snapshot: CapitalSnapshot,
    *,
    selected_leg_total_quote: Decimal,
    selected_native_minimum_quote: Decimal,
    decision_timestamp: float | None = None,
) -> CapitalSnapshot:
    """Re-evaluate only selected-leg affordability without mutating a snapshot."""

    available = snapshot.available_collateral_quote
    if available is None:
        return snapshot
    # Rebuild from the already-derived policy values so this helper remains
    # deterministic and cannot silently change any allocation cap.
    errors = [
        error
        for error in snapshot.errors
        if not error.startswith("selected_") and not error.startswith("native_minimum_")
    ]
    selected_total = _decimal(selected_leg_total_quote) or ZERO
    selected_minimum = _decimal(selected_native_minimum_quote) or ZERO
    if selected_total < ZERO:
        errors.append("selected_leg_total_invalid")
    if selected_minimum < ZERO:
        errors.append("selected_native_minimum_invalid")
    if selected_total + snapshot.fee_buffer_quote > snapshot.deployable_quote:
        errors.append("selected_leg_plus_fee_buffer_exceeds_deployable_collateral")
    if selected_minimum + snapshot.fee_buffer_quote > snapshot.deployable_quote:
        errors.append("native_minimum_plus_fee_buffer_exceeds_deployable_collateral")
    return replace(
        snapshot,
        selected_leg_total_quote=selected_total,
        selected_native_minimum_quote=selected_minimum,
        selected_leg_fundable=not any(
            error.startswith(("selected_", "native_minimum_")) for error in errors
        ),
        capital_ready=not errors,
        decision_timestamp=(
            snapshot.decision_timestamp if decision_timestamp is None else decision_timestamp
        ),
        errors=tuple(dict.fromkeys(errors)),
    )


__all__ = [
    "CapitalPolicy",
    "calculate_capital_snapshot",
    "with_selected_leg",
]
