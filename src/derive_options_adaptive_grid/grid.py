"""Pure grid geometry and sizing for the native Hummingbot GridExecutor."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .models import GridLegPlan, GridMode, GridPlan, InventorySnapshot, ModeDecision


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


@dataclass(frozen=True, slots=True)
class GridRules:
    aggressive_half_width_pct: Decimal = Decimal("0.0075")
    normal_half_width_pct: Decimal = Decimal("0.010")
    defensive_half_width_pct: Decimal = Decimal("0.025")
    aggressive_levels: int = 7
    normal_levels: int = 5
    defensive_levels: int = 3
    aggressive_total_quote: Decimal = Decimal("100")
    normal_total_quote: Decimal = Decimal("100")
    defensive_total_quote: Decimal = Decimal("60")
    min_order_amount_quote: Decimal = Decimal("5")
    # Native connector constraints are kept separate. In particular,
    # min_order_size_base is a quantity floor while base_amount_increment is
    # only the executable quantity tick.
    min_order_size_base: Decimal = Decimal("0")
    base_amount_increment: Decimal = Decimal("0")
    min_notional_quote: Decimal = Decimal("0")
    price_increment: Decimal = Decimal("0.01")
    quote_increment: Decimal = Decimal("0.01")
    activation_bounds_pct: Decimal | None = Decimal("0.025")
    order_frequency: int = 5
    max_orders_per_batch: int = 1
    min_spread_between_orders: Decimal = Decimal("0.0005")
    # Optional executable BBO guard. Pure callers that do not provide a BBO
    # retain the original geometry; the controller supplies both prices for
    # Derive so every maker level remains strictly non-crossing.
    best_bid: Decimal = Decimal("0")
    best_ask: Decimal = Decimal("0")
    safe_extra_spread: Decimal = Decimal("0")


DEFAULT_GRID_RULES = GridRules()


def _version(mode: GridMode, center: Decimal, legs: tuple[GridLegPlan, ...]) -> str:
    canonical = [mode.value, str(center)]
    for leg in legs:
        canonical.extend(
            [
                leg.side,
                str(leg.start_price),
                str(leg.end_price),
                str(leg.total_amount_quote),
                str(leg.min_order_amount_quote),
                str(leg.max_open_orders),
                str(leg.max_orders_per_batch),
                str(leg.min_spread_between_orders),
                str(leg.order_frequency),
                str(leg.activation_bounds),
                str(leg.limit_price),
                ",".join(str(price) for price in leg.expected_level_prices),
            ]
        )
    return hashlib.sha256("|".join(canonical).encode("utf-8")).hexdigest()[:16]


def _prices(
    start: Decimal, end: Decimal, levels: int, price_increment: Decimal
) -> tuple[Decimal, ...]:
    if levels == 1:
        return (_floor((start + end) / Decimal("2"), price_increment),)
    step = (end - start) / Decimal(levels - 1)
    prices = [
        start
        if index == 0
        else end
        if index == levels - 1
        else _floor(start + step * index, price_increment)
        for index in range(levels)
    ]
    # The plan is an executable-price diagnostic. Do not publish duplicate or
    # off-tick levels when a narrow range cannot support the requested count.
    normalized: list[Decimal] = []
    for price in prices:
        if normalized and price <= normalized[-1]:
            price = normalized[-1] + price_increment
        if price > end:
            return ()
        normalized.append(price)
    return tuple(normalized)


def _leg(
    *,
    side: str,
    center: Decimal,
    half_width: Decimal,
    levels: int,
    total_quote: Decimal,
    rules: GridRules,
) -> GridLegPlan | None:
    if total_quote <= 0 or levels < 1:
        return None
    if rules.price_increment <= 0:
        return None
    start = _floor(center * (Decimal("1") - half_width), rules.price_increment)
    end = _ceil(center * (Decimal("1") + half_width), rules.price_increment)

    if side == "BUY" and rules.best_ask > 0:
        maker_gap = max(rules.price_increment, rules.safe_extra_spread)
        end = min(end, _floor(rules.best_ask - maker_gap, rules.price_increment))
    elif side == "SELL" and rules.best_bid > 0:
        maker_gap = max(rules.price_increment, rules.safe_extra_spread)
        start = max(start, _ceil(rules.best_bid + maker_gap, rules.price_increment))

    if start <= 0 or end <= start:
        return None

    # Derive enforces both a minimum base quantity and a base quantity tick.
    # Quantize the minimum quantity first, then value it at the most expensive
    # price in this leg so every executable level remains above the native
    # exchange floor. The configured quote floor and native notional floor
    # remain independent constraints.
    min_base = rules.min_order_size_base
    if min_base > 0 and rules.base_amount_increment > 0:
        min_base = _ceil(min_base, rules.base_amount_increment)
    native_min_quote = max(
        rules.min_order_amount_quote,
        rules.min_notional_quote,
        min_base * end,
    )
    min_order_quote = (
        _ceil(native_min_quote, rules.quote_increment)
        if rules.quote_increment > 0
        else native_min_quote
    )
    if total_quote < min_order_quote:
        return None
    tick_capacity = int((end - start) / rules.price_increment) + 1
    effective_levels = min(
        levels,
        tick_capacity,
        int(total_quote / min_order_quote),
    )
    if effective_levels < 1:
        return None
    quote_per_level = _floor(total_quote / effective_levels, rules.quote_increment)
    if quote_per_level < min_order_quote:
        return None
    expected_level_prices = _prices(start, end, effective_levels, rules.price_increment)
    if not expected_level_prices:
        return None
    effective_levels = len(expected_level_prices)
    quote_per_level = _floor(total_quote / effective_levels, rules.quote_increment)
    if quote_per_level < min_order_quote:
        return None
    return GridLegPlan(
        side=side,
        start_price=start,
        end_price=end,
        # The installed GridExecutor treats a non-zero limit_price as a
        # position-stop condition. Zero deliberately disables that condition;
        # the controller owns the state-change stop/rebuild gate.
        limit_price=Decimal("0"),
        total_amount_quote=quote_per_level * effective_levels,
        min_order_amount_quote=min_order_quote,
        max_open_orders=effective_levels,
        max_orders_per_batch=min(rules.max_orders_per_batch, effective_levels),
        min_spread_between_orders=rules.min_spread_between_orders,
        order_frequency=rules.order_frequency,
        activation_bounds=rules.activation_bounds_pct,
        expected_level_prices=expected_level_prices,
    )


def build_grid_plan(
    *,
    center_price: Decimal | float | int | str,
    mode: ModeDecision,
    inventory: InventorySnapshot,
    rules: GridRules | None = None,
) -> GridPlan:
    """Create zero, one, or two side-specific plans; never submit an order."""

    rules = DEFAULT_GRID_RULES if rules is None else rules
    center = _decimal(center_price)
    if center <= 0:
        return GridPlan(
            plan_version="invalid",
            mode=mode.mode,
            center_price=center,
            decision_timestamp=mode.decision_timestamp,
            legs=(),
            valid=False,
            reason="invalid_center_price",
            errors=("center_price_non_positive",),
        )
    if inventory.hard_limit_reached:
        block_reasons = mode.reasons or ("hard_inventory_limit",)
        if inventory.hard_limit_reached:
            block_reasons = (*block_reasons, "hard_inventory_limit")
        return GridPlan(
            plan_version="invalid",
            mode=mode.mode,
            center_price=center,
            decision_timestamp=mode.decision_timestamp,
            legs=(),
            valid=False,
            reason=";".join(block_reasons),
            errors=tuple(dict.fromkeys(block_reasons)),
        )
    geometry = {
        GridMode.AGGRESSIVE: (
            rules.aggressive_half_width_pct,
            rules.aggressive_levels,
            rules.aggressive_total_quote,
        ),
        GridMode.NORMAL: (
            rules.normal_half_width_pct,
            rules.normal_levels,
            rules.normal_total_quote,
        ),
        GridMode.DEFENSIVE: (
            rules.defensive_half_width_pct,
            rules.defensive_levels,
            rules.defensive_total_quote,
        ),
    }
    half_width, levels, total_quote = geometry[mode.mode]
    enabled_side_count = int(mode.buy_allowed) + int(mode.sell_allowed)
    side_total_quote = total_quote / enabled_side_count if enabled_side_count else Decimal("0")
    legs: list[GridLegPlan] = []
    if mode.buy_allowed:
        leg = _leg(
            side="BUY",
            center=center,
            half_width=half_width,
            levels=levels,
            total_quote=side_total_quote,
            rules=rules,
        )
        if leg:
            legs.append(leg)
    if mode.sell_allowed:
        leg = _leg(
            side="SELL",
            center=center,
            half_width=half_width,
            levels=levels,
            total_quote=side_total_quote,
            rules=rules,
        )
        if leg:
            legs.append(leg)
    errors: list[str] = []
    if not legs:
        errors.append("no_valid_grid_legs")
    plan_legs = tuple(legs)
    valid = bool(plan_legs)
    return GridPlan(
        plan_version=_version(mode.mode, center, plan_legs) if valid else "invalid",
        mode=mode.mode,
        center_price=center,
        decision_timestamp=mode.decision_timestamp,
        legs=plan_legs,
        valid=valid,
        reason="grid_ready" if valid else ";".join(errors),
        errors=tuple(errors),
    )
