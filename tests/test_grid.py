from decimal import Decimal

from derive_options_adaptive_grid.grid import GridRules, build_grid_plan
from derive_options_adaptive_grid.models import (
    GridMode,
    InventorySnapshot,
    MarketState,
    ModeDecision,
)


def _mode(mode=GridMode.NORMAL, buy=True, sell=True):
    return ModeDecision(mode, MarketState.NORMAL, True, True, buy, sell, 1_700_000_000.0, ("test",))


def test_normal_plan_has_two_native_grid_legs():
    plan = build_grid_plan(center_price=100, mode=_mode(), inventory=InventorySnapshot())
    assert plan.valid is True
    assert {leg.side for leg in plan.legs} == {"BUY", "SELL"}
    assert plan.buy_leg is not None
    assert plan.buy_leg.limit_price == Decimal("0")
    assert plan.buy_leg.start_price < Decimal("100") < plan.buy_leg.end_price
    assert len(plan.buy_leg.expected_level_prices) == 5
    assert plan.buy_leg.total_amount_quote + plan.sell_leg.total_amount_quote <= Decimal("100")
    assert plan.decision_timestamp == 1_700_000_000.0


def test_defensive_plan_is_wider_and_smaller():
    normal = build_grid_plan(
        center_price=100, mode=_mode(GridMode.NORMAL), inventory=InventorySnapshot()
    )
    defensive = build_grid_plan(
        center_price=100, mode=_mode(GridMode.DEFENSIVE), inventory=InventorySnapshot()
    )
    assert (
        defensive.buy_leg.end_price - defensive.buy_leg.start_price
        > normal.buy_leg.end_price - normal.buy_leg.start_price
    )
    assert defensive.buy_leg.max_open_orders < normal.buy_leg.max_open_orders
    assert defensive.buy_leg.total_amount_quote < normal.buy_leg.total_amount_quote


def test_inventory_can_suppress_only_the_risk_increasing_side():
    plan = build_grid_plan(
        center_price=100,
        mode=_mode(buy=False, sell=True),
        inventory=InventorySnapshot(position_quote=20),
    )
    assert plan.valid is True
    assert plan.buy_leg is None
    assert plan.sell_leg is not None


def test_aggressive_plan_is_tighter_and_denser_without_more_quote():
    aggressive = build_grid_plan(
        center_price=100, mode=_mode(GridMode.AGGRESSIVE), inventory=InventorySnapshot()
    )
    normal = build_grid_plan(
        center_price=100, mode=_mode(GridMode.NORMAL), inventory=InventorySnapshot()
    )
    assert aggressive.valid is True
    assert len(aggressive.buy_leg.expected_level_prices) == 7
    assert len(normal.buy_leg.expected_level_prices) == 5
    assert (
        aggressive.buy_leg.end_price - aggressive.buy_leg.start_price
        < normal.buy_leg.end_price - normal.buy_leg.start_price
    )
    assert aggressive.buy_leg.total_amount_quote <= normal.buy_leg.total_amount_quote


def test_invalid_center_retains_intended_mode_and_produces_no_executor_legs():
    plan = build_grid_plan(
        center_price=0, mode=_mode(GridMode.DEFENSIVE), inventory=InventorySnapshot()
    )
    assert plan.valid is False
    assert plan.legs == ()
    assert plan.mode == GridMode.DEFENSIVE


def test_executor_relevant_change_changes_plan_version():
    baseline = build_grid_plan(center_price=100, mode=_mode(), inventory=InventorySnapshot())
    changed = build_grid_plan(
        center_price=100,
        mode=_mode(),
        inventory=InventorySnapshot(),
        rules=GridRules(order_frequency=9),
    )
    assert baseline.plan_version != changed.plan_version


def test_native_minimum_reduces_levels_for_each_grid_mode():
    rules = GridRules(
        aggressive_total_quote=Decimal("30"),
        normal_total_quote=Decimal("30"),
        defensive_total_quote=Decimal("30"),
        min_order_amount_quote=Decimal("5"),
        min_order_size_base=Decimal("0.1"),
        base_amount_increment=Decimal("0.001"),
        quote_increment=Decimal("0.01"),
    )
    requested_levels = {
        GridMode.AGGRESSIVE: rules.aggressive_levels,
        GridMode.NORMAL: rules.normal_levels,
        GridMode.DEFENSIVE: rules.defensive_levels,
    }

    for mode, requested in requested_levels.items():
        plan = build_grid_plan(
            center_price=100,
            mode=_mode(mode),
            inventory=InventorySnapshot(),
            rules=rules,
        )
        assert plan.valid is True
        assert plan.buy_leg is not None
        assert plan.buy_leg.max_open_orders < requested
        assert plan.buy_leg.min_order_amount_quote >= Decimal("10")


def test_bbo_guard_applies_the_configured_absolute_maker_gap():
    rules = GridRules(
        price_increment=Decimal("0.1"),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
        safe_extra_spread=Decimal("0.3"),
    )
    plan = build_grid_plan(
        center_price=100,
        mode=_mode(),
        inventory=InventorySnapshot(),
        rules=rules,
    )
    assert plan.buy_leg is not None
    assert plan.sell_leg is not None
    assert plan.buy_leg.end_price <= Decimal("100.7")
    assert plan.sell_leg.start_price >= Decimal("100.3")
