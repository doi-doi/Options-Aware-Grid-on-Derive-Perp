from derive_options_adaptive_grid.models import (
    GridMode,
    InventorySnapshot,
    MarketState,
    OptionsSnapshot,
    PerpSnapshot,
    RiskGateState,
)
from derive_options_adaptive_grid.modes import determine_mode
from derive_options_adaptive_grid.state import CausalIVState, StateConfig


def _snapshot(now: float, iv: float, source: float | None = None, available: bool = True):
    return OptionsSnapshot(
        underlying="SOL",
        reference_price=100.0,
        expiry_timestamp=now + 7 * 86_400,
        expiry="2023-11-21",
        days_to_expiry=7,
        atm_strike=100,
        atm_distance_pct=0,
        call_instrument="C",
        put_instrument="P",
        call_iv=iv,
        put_iv=iv,
        atm_iv=iv if available else None,
        call_iv_source="mark_iv",
        put_iv_source="mark_iv",
        source_timestamp=source if source is not None else now - 1,
        received_timestamp=now - 0.5,
        decision_timestamp=now,
        source="test",
        environment="mainnet",
        data_available=available,
        confidence=1.0 if available else 0.0,
        errors=() if available else ("missing",),
    )


def test_history_is_causal_and_does_not_forward_fill():
    now = 1_700_000_000.0
    state = CausalIVState(StateConfig(min_history=2, max_option_age_seconds=10))
    assert (
        state.observe(_snapshot(now, 0.5, now - 1), decision_timestamp=now).state
        == MarketState.INITIALIZING
    )
    assert (
        state.observe(_snapshot(now + 1, 0.5, now), decision_timestamp=now + 1).state
        == MarketState.NORMAL
    )
    missing = state.observe(
        _snapshot(now + 2, 0.9, None, available=False), decision_timestamp=now + 2
    )
    assert missing.accepted is False
    assert missing.current_iv is None
    assert len(state.observations) == 2


def test_out_of_order_is_rejected():
    now = 1_700_000_000.0
    state = CausalIVState(StateConfig(min_history=1))
    state.observe(_snapshot(now, 0.5, now - 1), decision_timestamp=now)
    result = state.observe(_snapshot(now + 1, 0.6, now - 2), decision_timestamp=now + 1)
    assert result.accepted is False
    assert "options_observation_out_of_order" in result.reasons


def test_high_iv_is_defensive_and_unready_options_keep_intended_mode():
    now = 1_700_000_000.0
    state = CausalIVState(StateConfig(min_history=2, high_enter_ratio=1.2))
    state.observe(_snapshot(now, 0.5, now - 2), decision_timestamp=now)
    normal = state.observe(_snapshot(now + 1, 0.5, now - 1), decision_timestamp=now + 1)
    high = state.observe(_snapshot(now + 2, 0.75, now), decision_timestamp=now + 2)
    risk = RiskGateState(True, True, True, True, True, False, ())
    normal_mode = determine_mode(
        state=normal.state,
        perp_market_ready=True,
        options=_snapshot(now + 1, 0.5, now),
        inventory=InventorySnapshot(),
        risk=risk,
        decision_timestamp=now + 1,
    )
    high_mode = determine_mode(
        state=high.state,
        perp_market_ready=True,
        options=_snapshot(now + 2, 0.75, now + 1),
        inventory=InventorySnapshot(),
        risk=risk,
        decision_timestamp=now + 2,
    )
    paused_mode = determine_mode(
        state=high.state,
        perp_market_ready=True,
        options=_snapshot(now + 2, 0.75, now - 100, available=False),
        inventory=InventorySnapshot(),
        risk=risk,
        decision_timestamp=now + 2,
    )
    assert normal_mode.mode.value == "NORMAL"
    assert high_mode.mode.value == "DEFENSIVE"
    assert paused_mode.mode == GridMode.DEFENSIVE
    assert paused_mode.buy_allowed is True
    assert "options_data_unavailable" in paused_mode.reasons


def test_mode_rechecks_current_age_and_keeps_readiness_independent():
    now = 1_700_000_000.0
    state = RiskGateState(True, True, True, True, True, False, ())
    options = _snapshot(now, 0.5, now - 1)
    stale = determine_mode(
        state=MarketState.NORMAL,
        perp_market_ready=True,
        options=options,
        inventory=InventorySnapshot(),
        risk=state,
        decision_timestamp=now + 20,
    )
    perp_not_ready = determine_mode(
        state=MarketState.NORMAL,
        perp_market_ready=False,
        options=_snapshot(now, 0.5, now - 1),
        inventory=InventorySnapshot(),
        risk=state,
        decision_timestamp=now,
    )
    assert stale.mode == GridMode.NORMAL
    assert stale.perp_market_ready is True
    assert stale.options_data_available is True
    assert perp_not_ready.mode == GridMode.NORMAL
    assert perp_not_ready.perp_market_ready is False
    assert perp_not_ready.options_data_available is True


def test_wrong_environment_and_future_receipt_fail_closed():
    now = 1_700_000_000.0
    risk = RiskGateState(True, True, True, True, True, False, ())
    wrong_environment = _snapshot(now, 0.5, now - 1)
    object.__setattr__(wrong_environment, "environment", "testnet")
    future_receipt = _snapshot(now, 0.5, now - 1)
    object.__setattr__(future_receipt, "received_timestamp", now + 10)
    for options in (wrong_environment, future_receipt):
        decision = determine_mode(
            state=MarketState.NORMAL,
            perp_market_ready=True,
            options=options,
            inventory=InventorySnapshot(),
            risk=risk,
            decision_timestamp=now,
        )
        assert decision.mode == GridMode.NORMAL


def test_high_and_extreme_states_have_exit_hysteresis():
    now = 1_700_000_000.0
    state = CausalIVState(
        StateConfig(
            min_history=2,
            high_enter_ratio=1.2,
            high_exit_ratio=1.1,
            extreme_enter_ratio=1.6,
            extreme_exit_ratio=1.3,
        )
    )
    assert (
        state.observe(_snapshot(now, 0.5, now - 4), decision_timestamp=now).state
        == MarketState.INITIALIZING
    )
    assert (
        state.observe(_snapshot(now + 1, 0.5, now - 3), decision_timestamp=now + 1).state
        == MarketState.NORMAL
    )
    assert (
        state.observe(_snapshot(now + 2, 0.7, now - 2), decision_timestamp=now + 2).state
        == MarketState.HIGH
    )
    assert (
        state.observe(_snapshot(now + 3, 0.85, now - 1), decision_timestamp=now + 3).state
        == MarketState.EXTREME
    )
    assert (
        state.observe(_snapshot(now + 4, 0.8, now), decision_timestamp=now + 4).state
        == MarketState.EXTREME
    )
    assert (
        state.observe(_snapshot(now + 5, 0.5, now + 1), decision_timestamp=now + 5).state
        == MarketState.LOW
    )


def test_perpetual_book_requires_a_strict_bid_ask_spread():
    equal_book = PerpSnapshot(
        connector_name="derive_perpetual",
        trading_pair="SOL-USDC",
        exchange_instrument="SOL-PERP",
        bid=100,
        ask=100,
        source_timestamp=1,
        received_timestamp=1,
        decision_timestamp=1,
    )
    valid_book = PerpSnapshot(
        connector_name="derive_perpetual",
        trading_pair="SOL-USDC",
        exchange_instrument="SOL-PERP",
        bid=100,
        ask=100.01,
        source_timestamp=1,
        received_timestamp=1,
        decision_timestamp=1,
    )
    assert equal_book.market_ready is False
    assert valid_book.market_ready is True


def test_state_config_rejects_invalid_hysteresis_ordering():
    import pytest

    with pytest.raises(ValueError, match="state ratios"):
        StateConfig(high_exit_ratio=1.30, high_enter_ratio=1.20)

    with pytest.raises(ValueError, match="state ratios"):
        StateConfig(extreme_exit_ratio=1.10, high_enter_ratio=1.20)

    with pytest.raises(ValueError, match="state ratios"):
        StateConfig(aggressive_enter_ratio=0.98, aggressive_exit_ratio=0.95)


def test_low_iv_hysteresis_and_mode_mapping():
    now = 1_700_000_000.0
    state = CausalIVState(StateConfig(min_history=2))
    state.observe(_snapshot(now, 1.0, now - 2), decision_timestamp=now)
    normal = state.observe(_snapshot(now + 1, 1.0, now - 1), decision_timestamp=now + 1)
    low = state.observe(_snapshot(now + 2, 0.85, now), decision_timestamp=now + 2)
    retained = state.observe(_snapshot(now + 3, 0.90, now + 1), decision_timestamp=now + 3)
    exited = state.observe(_snapshot(now + 4, 0.95, now + 2), decision_timestamp=now + 4)
    risk = RiskGateState(True, True, True, True, True, False, ())
    assert normal.state == MarketState.NORMAL
    assert low.state == MarketState.LOW
    assert retained.state == MarketState.LOW
    assert exited.state == MarketState.NORMAL
    decision = determine_mode(
        state=MarketState.LOW,
        perp_market_ready=True,
        options=_snapshot(now + 4, 0.95, now + 3),
        inventory=InventorySnapshot(),
        risk=risk,
        decision_timestamp=now + 4,
    )
    assert decision.mode == GridMode.AGGRESSIVE
