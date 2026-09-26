from decimal import Decimal

from derive_options_adaptive_grid.capital import CapitalPolicy, calculate_capital_snapshot


def _policy(**overrides):
    values = {
        "total_amount_quote": Decimal("100"),
        "aggressive_total_quote": Decimal("100"),
        "defensive_total_quote": Decimal("60"),
        "max_position_quote": Decimal("50"),
        "hard_position_quote": Decimal("75"),
        "max_order_notional_quote": Decimal("100"),
        "reserve_pct": Decimal("0.20"),
        "reserve_quote": Decimal("20"),
        "utilization_pct": Decimal("0.50"),
        "fee_buffer_pct": Decimal("0.02"),
        "fee_buffer_quote": Decimal("5"),
        "hard_position_multiplier": Decimal("1.25"),
    }
    values.update(overrides)
    return CapitalPolicy(**values)


def test_current_wallet_allocation_is_decimal_and_capped():
    snapshot = calculate_capital_snapshot(Decimal("98.90"), _policy())

    assert snapshot.capital_ready is True
    assert snapshot.reserve_quote == Decimal("20")
    assert snapshot.deployable_quote == Decimal("78.90")
    assert snapshot.target_strategy_quote == Decimal("39.450")
    assert snapshot.aggressive_budget_quote == snapshot.normal_budget_quote
    assert snapshot.defensive_budget_quote == Decimal("23.6700")
    assert snapshot.dynamic_soft_position_quote == Decimal("39.450")
    assert snapshot.dynamic_hard_position_quote == Decimal("49.31250")
    assert snapshot.fee_buffer_quote == Decimal("5")


def test_large_wallet_never_increases_static_caps():
    snapshot = calculate_capital_snapshot(Decimal("10000"), _policy())

    assert snapshot.target_strategy_quote == Decimal("100")
    assert snapshot.aggressive_budget_quote == Decimal("100")
    assert snapshot.normal_budget_quote == Decimal("100")
    assert snapshot.defensive_budget_quote == Decimal("60")
    assert snapshot.dynamic_soft_position_quote == Decimal("50")
    assert snapshot.dynamic_hard_position_quote == Decimal("62.50")


def test_small_or_invalid_wallet_fails_closed():
    for available in (Decimal("19.99"), Decimal("0"), None, "not-a-number", Decimal("-1")):
        snapshot = calculate_capital_snapshot(available, _policy())
        assert snapshot.capital_ready is False
        assert snapshot.target_strategy_quote == Decimal("0")


def test_selected_leg_and_fee_buffer_must_fit_deployable_collateral():
    snapshot = calculate_capital_snapshot(
        Decimal("98.90"),
        _policy(),
        selected_leg_total_quote=Decimal("75"),
        selected_native_minimum_quote=Decimal("20"),
    )

    assert snapshot.capital_ready is False
    assert snapshot.selected_leg_fundable is False
    assert "selected_leg_plus_fee_buffer_exceeds_deployable_collateral" in snapshot.errors


def test_aggressive_budget_cannot_exceed_normal_budget():
    snapshot = calculate_capital_snapshot(
        Decimal("500"),
        _policy(aggressive_total_quote=Decimal("100")),
    )
    assert snapshot.aggressive_budget_quote <= snapshot.normal_budget_quote


def test_hard_cap_is_never_below_soft_cap():
    snapshot = calculate_capital_snapshot(Decimal("98.90"), _policy())
    assert snapshot.dynamic_hard_position_quote >= snapshot.dynamic_soft_position_quote
