"""Runtime-only controller contract tests.

The normal package test environment intentionally has no Hummingbot dependency.
The same file is run in the installed Hummingbot Python environment by the
release verification step.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

try:
    from controllers.market_making.derive_options_adaptive_grid import (
        DeriveOptionsAdaptiveGrid,
        DeriveOptionsAdaptiveGridConfig,
    )
    from derive_options_adaptive_grid.grid import GridRules, build_grid_plan
    from derive_options_adaptive_grid.models import (
        CapitalSnapshot,
        GridMode,
        InventorySnapshot,
        MarketState,
        ModeDecision,
        OptionsSnapshot,
        RiskGateState,
    )
    from derive_options_adaptive_grid.research.models import CalibrationObservation
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in runtime container
    pytest.skip(f"Hummingbot runtime dependency unavailable: {exc}", allow_module_level=True)


class FakeProvider:
    def __init__(self):
        self.now = 1_700_000_000.0
        self.connector = FakeConnector()

    def time(self):
        return self.now

    def initialize_rate_sources(self, _pairs):
        return None

    def get_price_by_type(self, _connector_name, _trading_pair, price_type):
        return Decimal("100") if price_type.name == "BestBid" else Decimal("101")

    def get_connector(self, _connector_name):
        return self.connector

    def get_trading_rules(self, _connector_name, _trading_pair):
        return self.connector.trading_rule


class FakeOrderBook:
    snapshot_uid = 1
    last_update_id = 1

    @staticmethod
    def bid_entries():
        return iter([SimpleNamespace(price=Decimal("100"))])

    @staticmethod
    def ask_entries():
        return iter([SimpleNamespace(price=Decimal("101"))])


class FakeConnector:
    ready = True
    account_positions = {}
    in_flight_orders = {}
    available_balances = {"USDC": Decimal("1000")}
    _instrument_ticker = [
        {
            "instrument_name": "SOL-PERP",
            "maker_fee_rate": "0.0001",
            "taker_fee_rate": "0.0003",
        }
    ]
    trading_rule = type(
        "Rule",
        (),
        {
            "min_price_increment": Decimal("0.01"),
            "min_quote_amount_increment": Decimal("0.01"),
            "min_notional_size": Decimal("5"),
            "min_order_size": Decimal("0.01"),
            "min_base_amount_increment": Decimal("0.01"),
        },
    )()

    def get_order_book(self, _trading_pair):
        return FakeOrderBook()

    def get_available_balance(self, asset):
        return self.available_balances[asset]


class FakeOptionsProvider:
    def __init__(self, source_timestamp):
        self.source_timestamp = source_timestamp
        self.calls = 0

    async def snapshot(self, reference_price):
        self.calls += 1
        return OptionsSnapshot(
            underlying="SOL",
            reference_price=reference_price,
            expiry_timestamp=1_700_000_000.0 + 7 * 86_400,
            expiry="2023-11-21",
            days_to_expiry=7,
            atm_strike=100,
            atm_distance_pct=0,
            call_strike=100,
            put_strike=100,
            call_instrument="SOL-C",
            put_instrument="SOL-P",
            call_iv=0.5,
            put_iv=0.5,
            atm_iv=0.5,
            call_iv_source="mark_iv",
            put_iv_source="mark_iv",
            source_timestamp=self.source_timestamp,
            received_timestamp=self.source_timestamp + 0.1,
            decision_timestamp=self.source_timestamp + 0.2,
            source="test",
            environment="mainnet",
            data_available=True,
            confidence=1.0,
        )


class IncrementingOptionsProvider(FakeOptionsProvider):
    async def snapshot(self, reference_price):
        self.source_timestamp += 1
        return await super().snapshot(reference_price)


def test_config_is_exact_and_disarmed_by_default():
    config = DeriveOptionsAdaptiveGridConfig()
    assert config.connector_name == "derive_perpetual"
    assert config.trading_pair == "SOL-USDC"
    assert config.exchange_instrument == "SOL-PERP"
    assert config.environment == "mainnet"
    assert config.mainnet_armed is False
    assert config.execution_enabled is False
    assert config.manual_kill_switch is False
    assert DeriveOptionsAdaptiveGridConfig.model_fields["mainnet_armed"].json_schema_extra == {
        "is_updatable": True
    }
    assert DeriveOptionsAdaptiveGridConfig.model_fields["manual_kill_switch"].json_schema_extra == {
        "is_updatable": True
    }
    assert config.high_enter_ratio == 1.25
    assert config.high_exit_ratio == 1.12
    assert config.extreme_enter_ratio == 1.60
    assert config.extreme_exit_ratio == 1.35


@pytest.mark.parametrize(
    "field",
    [
        "connector_close_semantics_verified",
        "connector_post_only_semantics_verified",
        "connector_lifecycle_verified",
    ],
)
def test_config_cannot_assert_connector_runtime_proof(field):
    with pytest.raises(ValueError, match="runtime evidence only"):
        DeriveOptionsAdaptiveGridConfig(**{field: True})


@pytest.mark.parametrize(
    "overrides",
    [
        {"high_exit_ratio": 1.30, "high_enter_ratio": 1.20},
        {"high_enter_ratio": 1.40, "extreme_exit_ratio": 1.30},
        {"extreme_exit_ratio": 1.70, "extreme_enter_ratio": 1.60},
    ],
)
def test_config_rejects_invalid_state_hysteresis(overrides):
    with pytest.raises(ValueError, match="state ratios"):
        DeriveOptionsAdaptiveGridConfig(**overrides)


def test_configured_state_policy_reaches_state_machine_and_diagnostics():
    config = DeriveOptionsAdaptiveGridConfig(
        high_enter_ratio=1.30,
        high_exit_ratio=1.15,
        extreme_enter_ratio=1.80,
        extreme_exit_ratio=1.35,
    )
    controller = DeriveOptionsAdaptiveGrid(config, FakeProvider(), None)
    assert controller.iv_state.config.high_enter_ratio == 1.30
    assert controller.iv_state.config.high_exit_ratio == 1.15
    info = controller.get_custom_info()
    assert info["state_policy"]["extreme_enter_ratio"] == 1.80
    assert "high_exit_ratio" in info["state_policy"]["invariant"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("connector_name", "derive_perpetual_testnet"),
        ("trading_pair", "BTC-USDC"),
        ("environment", "testnet"),
    ],
)
def test_config_rejects_scope_changes(field, value):
    with pytest.raises(ValueError):
        DeriveOptionsAdaptiveGridConfig(**{field: value})


def test_unarmed_controller_returns_no_mutation_actions():
    controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), FakeProvider(), None)
    assert controller.determine_executor_actions() == []
    info = controller.get_custom_info()
    assert info["execution"]["execution_enabled"] is False
    assert info["execution"]["mainnet_armed"] is False
    assert info["risk_gates"]["ready"] is False


def test_update_is_read_only_and_duplicate_iv_timestamp_does_not_grow_history():
    async def exercise():
        provider = FakeProvider()
        config = DeriveOptionsAdaptiveGridConfig(options_refresh_interval_seconds=0.1)
        controller = DeriveOptionsAdaptiveGrid(config, provider, None)
        controller.options_provider = FakeOptionsProvider(provider.now - 1)
        await controller.update_processed_data()
        first_history = len(controller.iv_state.observations)
        provider.now += 1
        await controller.update_processed_data()
        assert controller.options_provider.calls == 2
        assert len(controller.iv_state.observations) == first_history
        assert controller.determine_executor_actions() == []
        assert controller.get_custom_info()["perp_market_ready"] is True
        assert controller.get_custom_info()["options_data_available"] is True

    asyncio.run(exercise())


def test_disarmed_controller_keeps_shadow_grid_plan_and_records_would_create():
    async def exercise():
        provider = FakeProvider()
        config = DeriveOptionsAdaptiveGridConfig(
            state_min_history=2,
            options_refresh_interval_seconds=0.1,
        )
        controller = DeriveOptionsAdaptiveGrid(config, provider, None)
        controller.options_provider = IncrementingOptionsProvider(provider.now - 2)
        await controller.update_processed_data()
        provider.now += 1
        await controller.update_processed_data()

        actions = controller.determine_executor_actions()
        info = controller.get_custom_info()

        assert actions == []
        assert info["market_state"] == "NORMAL"
        assert info["grid_mode"] == "NORMAL"
        assert info["grid"]["valid"] is True
        assert info["grid"]["selected_leg"] is not None
        assert info["risk_gates"]["planning_ready"] is True
        assert info["risk_gates"]["ready"] is False
        assert info["risk_gates"]["lifecycle_ready"] is False
        assert info["connector_proof"]["config_assertions_allowed"] is False
        assert info["can_create_executor_now"] is False
        assert info["execution"]["manual_kill_switch"] is False
        assert info["evidence"]["counts"]["WOULD_CREATE"] == 1

        if info["connector_proof"]["contract_verified"]:
            assert info["risk_gates"]["fee_economics_ready"] is True
            assert info["risk_gates"]["entry_semantics_ready"] is True
            assert info["risk_gates"]["exit_semantics_ready"] is True
            assert "BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE" in info["risk_gates"]["reasons"]
            assert info["fee_economics"]["fee_model_status"] == "VERIFIED"
            assert info["fee_economics"]["fee_source_mismatch"] is False
            assert info["fee_economics"]["minimum_economic_take_profit_pct"] is not None
            assert not info["fee_economics"]["errors"]
            assert info["connector_proof"]["status"] == "CONTRACT_VERIFIED"
        else:
            assert info["risk_gates"]["fee_economics_ready"] is False
            assert info["risk_gates"]["entry_semantics_ready"] is False
            assert info["risk_gates"]["exit_semantics_ready"] is False
            assert "BLOCKED_BY_FEE_MODEL" in info["risk_gates"]["reasons"]
            assert info["fee_economics"]["fee_model_status"] == "UNKNOWN"
            assert info["fee_economics"]["fee_source_mismatch"] is True
            assert info["fee_economics"]["minimum_economic_take_profit_pct"] is None
            assert "fee_source_mismatch" in info["fee_economics"]["errors"]
            assert info["connector_proof"]["status"] == "UNVERIFIED"

    asyncio.run(exercise())


def test_controller_exports_validator_backed_calibration_observation():
    async def exercise():
        provider = FakeProvider()
        controller = DeriveOptionsAdaptiveGrid(
            DeriveOptionsAdaptiveGridConfig(state_min_history=2), provider, None
        )
        controller.options_provider = IncrementingOptionsProvider(provider.now - 2)
        await controller.update_processed_data()
        info = controller.get_custom_info()

        assert info["calibration_observation_ready"] is True
        observation = CalibrationObservation.from_mapping(info["calibration_observation"])
        assert observation.validation_errors() == ()
        assert observation.option_reference_price == 100.5
        assert observation.call_strike == observation.put_strike == observation.atm_strike == 100
        assert observation.call_iv_source == "mark_iv"
        assert observation.put_iv_source == "mark_iv"
        assert observation.evidence == "SHADOW_PLAN"
        assert "native_order_id" not in info["calibration_observation"]

    asyncio.run(exercise())


def test_controller_fails_closed_when_perp_is_unready():
    async def exercise():
        provider = FakeProvider()

        def broken_price(*_args, **_kwargs):
            raise RuntimeError("unavailable")

        provider.get_price_by_type = broken_price
        controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), provider, None)
        controller.options_provider = FakeOptionsProvider(provider.now - 1)
        await controller.update_processed_data()
        info = controller.get_custom_info()

        assert info["perp_market_ready"] is False
        assert info["calibration_observation_ready"] is False
        assert info["calibration_observation"] is None
        assert any(
            "perpetual_read_error" in error for error in info["calibration_observation_errors"]
        )

    asyncio.run(exercise())


def test_controller_fails_closed_when_options_are_unready():
    async def exercise():
        provider = FakeProvider()
        controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), provider, None)

        class UnavailableOptionsProvider:
            """Async fixture matching the provider boundary."""

            async def snapshot(self, _reference_price):
                return OptionsSnapshot(
                    underlying="SOL",
                    reference_price=100.5,
                    expiry_timestamp=None,
                    expiry=None,
                    days_to_expiry=None,
                    atm_strike=None,
                    atm_distance_pct=None,
                    call_instrument=None,
                    put_instrument=None,
                    call_iv=None,
                    put_iv=None,
                    atm_iv=None,
                    call_iv_source=None,
                    put_iv_source=None,
                    source_timestamp=None,
                    received_timestamp=None,
                    decision_timestamp=provider.now,
                    source="test",
                    environment="mainnet",
                    data_available=False,
                    confidence=0.0,
                    errors=("options_unavailable",),
                )

        controller.options_provider = UnavailableOptionsProvider()
        await controller.update_processed_data()
        info = controller.get_custom_info()

        assert info["options_data_available"] is False
        assert info["calibration_observation_ready"] is False
        assert info["calibration_observation"] is None
        assert "options_unavailable" in info["calibration_observation_errors"]

    asyncio.run(exercise())


def test_native_grid_config_maps_stable_side_and_maker_barriers():
    controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), FakeProvider(), None)
    controller._trading_rule = type(
        "Rule",
        (),
        {
            "min_price_increment": 0.01,
            "min_quote_amount_increment": 0.01,
            "min_notional_size": 5,
            "min_order_size": 0.01,
        },
    )()
    mode = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    plan = build_grid_plan(
        center_price=100,
        mode=mode,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )
    config = controller._native_config(plan.buy_leg, plan.decision_timestamp)
    assert config.id != "sol_grid_buy"
    assert config.level_id == "sol_grid_buy"
    assert config.side.name == "BUY"
    assert config.triple_barrier_config.open_order_type.name == "LIMIT_MAKER"
    assert config.triple_barrier_config.take_profit_order_type.name == "LIMIT_MAKER"
    assert config.keep_position is True
    assert config.triple_barrier_config.time_limit is None


def test_grid_budget_is_capped_by_projected_hard_inventory_headroom():
    controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), FakeProvider(), None)
    controller._trading_rule = FakeConnector.trading_rule
    controller._capital_snapshot = CapitalSnapshot(
        target_strategy_quote=Decimal("100"),
        aggressive_budget_quote=Decimal("100"),
        normal_budget_quote=Decimal("100"),
        defensive_budget_quote=Decimal("60"),
    )
    controller._inventory = InventorySnapshot(
        position_quote=Decimal("40"),
        max_position_quote=Decimal("50"),
        hard_position_quote=Decimal("75"),
    )

    rules = controller._grid_rules(Decimal("100"))

    assert rules.normal_total_quote == Decimal("10")
    assert rules.defensive_total_quote == Decimal("10")


def test_grid_minimum_uses_native_min_order_size_not_base_increment():
    controller = DeriveOptionsAdaptiveGrid(
        DeriveOptionsAdaptiveGridConfig(
            max_position_quote=Decimal("100"),
            hard_position_quote=Decimal("100"),
        ),
        FakeProvider(),
        None,
    )
    controller._trading_rule = type(
        "Rule",
        (),
        {
            "min_price_increment": Decimal("0.01"),
            "min_quote_amount_increment": Decimal("0.01"),
            "min_notional_size": Decimal("0"),
            "min_order_size": Decimal("0.1"),
            "min_base_amount_increment": Decimal("0.001"),
        },
    )()
    controller._capital_snapshot = CapitalSnapshot(
        target_strategy_quote=Decimal("100"),
        aggressive_budget_quote=Decimal("100"),
        normal_budget_quote=Decimal("100"),
        defensive_budget_quote=Decimal("60"),
    )
    controller._inventory = InventorySnapshot(
        max_position_quote=Decimal("100"),
        hard_position_quote=Decimal("100"),
    )

    rules = controller._grid_rules(Decimal("100"))

    assert rules.min_order_amount_quote == Decimal("5")
    assert rules.min_order_size_base == Decimal("0.1")
    assert rules.base_amount_increment == Decimal("0.001")
    plan = build_grid_plan(
        center_price=100,
        mode=ModeDecision(
            GridMode.NORMAL,
            MarketState.NORMAL,
            True,
            True,
            True,
            True,
            1_700_000_000.0,
            (),
        ),
        inventory=InventorySnapshot(),
        rules=rules,
    )
    assert plan.buy_leg is not None
    assert plan.buy_leg.min_order_amount_quote == Decimal("10.10")
    assert plan.buy_leg.max_open_orders == 4


def test_headroom_below_minimum_order_suppresses_grid_with_explicit_reason():
    controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), FakeProvider(), None)
    controller._trading_rule = FakeConnector.trading_rule
    controller._inventory = InventorySnapshot(
        position_quote=Decimal("46"),
        max_position_quote=Decimal("50"),
        hard_position_quote=Decimal("75"),
    )

    rules = controller._grid_rules(Decimal("100"))
    plan = build_grid_plan(
        center_price=100,
        mode=ModeDecision(
            GridMode.NORMAL,
            MarketState.NORMAL,
            True,
            True,
            True,
            True,
            1_700_000_000.0,
            (),
        ),
        inventory=controller._inventory,
        rules=rules,
    )

    assert controller._grid_size_block_reason == (
        "projected_inventory_headroom_below_minimum_grid_order"
    )
    assert plan.valid is False


def test_foreign_executor_with_matching_level_id_is_unmanaged_and_blocks_creation():
    config = DeriveOptionsAdaptiveGridConfig(
        execution_enabled=True,
        mainnet_armed=True,
        manual_kill_switch=False,
    )
    controller = DeriveOptionsAdaptiveGrid(config, FakeProvider(), None)
    controller._mode_decision = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    controller._grid_plan = build_grid_plan(
        center_price=100,
        mode=controller._mode_decision,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )
    controller._risk = RiskGateState(True, True, True, True, True, False, ())
    foreign = SimpleNamespace(
        id="foreign-executor",
        type="grid_executor",
        controller_id="another_controller",
        config=SimpleNamespace(level_id="sol_grid_buy"),
    )
    controller.executors_info = [foreign]
    controller._active_grid_executors = lambda: [foreign]

    actions = controller.determine_executor_actions()

    assert actions == []
    assert controller._last_reconciliation["unmanaged"] == 1
    assert controller._last_reconciliation["creates"] == 0


def test_native_configs_use_unique_executor_ids_with_stable_level_ids():
    controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), FakeProvider(), None)
    controller._trading_rule = FakeConnector.trading_rule
    mode = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    plan = build_grid_plan(
        center_price=100,
        mode=mode,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )

    first = controller._native_config(plan.buy_leg, plan.decision_timestamp)
    second = controller._native_config(plan.buy_leg, plan.decision_timestamp + 1)

    assert first.level_id == second.level_id == "sol_grid_buy"
    assert first.id != second.id


def test_stop_before_create_is_keep_position_only_under_derive_connector_semantics():
    config = DeriveOptionsAdaptiveGridConfig(
        execution_enabled=True,
        mainnet_armed=True,
        manual_kill_switch=False,
    )
    controller = DeriveOptionsAdaptiveGrid(config, FakeProvider(), None)
    controller._mode_decision = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    controller._grid_plan = build_grid_plan(
        center_price=100,
        mode=controller._mode_decision,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )
    controller._risk = RiskGateState(True, True, True, True, True, False, ())
    managed_record = SimpleNamespace(
        id="managed-executor",
        type="grid_executor",
        controller_id=config.id,
        config=SimpleNamespace(level_id="sol_grid_buy"),
    )
    managed_active_copy = SimpleNamespace(
        id="managed-executor",
        type="grid_executor",
        controller_id=config.id,
        config=SimpleNamespace(level_id="sol_grid_buy"),
    )
    controller.executors_info = [managed_record]
    controller._active_grid_executors = lambda: [managed_active_copy]
    controller._executor_matches = lambda *_args: False

    actions = controller.determine_executor_actions()

    assert len(actions) == 1
    assert type(actions[0]).__name__ == "StopExecutorAction"
    assert actions[0].keep_position is True
    assert controller._last_reconciliation["creates"] == 0


@pytest.mark.parametrize(
    "blocked_field,blocked_value",
    [
        ("market_data_ready", False),
        ("options_ready", False),
        ("state_ready", False),
        ("pricing_ready", False),
        ("collateral_ready", False),
        ("inventory_ready", False),
        ("executor_ready", False),
        ("manual_kill_clear", False),
        ("connector_ready", False),
        ("grid_size_ready", False),
        ("capital_ready", False),
        ("fee_economics_ready", False),
        ("entry_semantics_ready", False),
        ("exit_semantics_ready", False),
        ("lifecycle_ready", False),
        ("hard_block", True),
    ],
)
def test_active_executor_safety_gate_stops_once_without_same_cycle_create(
    blocked_field, blocked_value
):
    config = DeriveOptionsAdaptiveGridConfig(
        execution_enabled=True,
        mainnet_armed=True,
        manual_kill_switch=False,
    )
    controller = DeriveOptionsAdaptiveGrid(config, FakeProvider(), None)
    controller._mode_decision = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    controller._grid_plan = build_grid_plan(
        center_price=100,
        mode=controller._mode_decision,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )
    base_risk = RiskGateState(True, True, True, True, True, False, ())
    controller._risk = replace(base_risk, **{blocked_field: blocked_value})
    managed_record = SimpleNamespace(
        id="managed-executor",
        type="grid_executor",
        controller_id=config.id,
        config=SimpleNamespace(level_id="sol_grid_buy"),
    )
    managed_active_copy = SimpleNamespace(
        id="managed-executor",
        type="grid_executor",
        controller_id=config.id,
        config=SimpleNamespace(level_id="sol_grid_buy"),
    )
    controller.executors_info = [managed_record]
    controller._active_grid_executors = lambda: [managed_active_copy]
    controller._executor_matches = lambda *_args: True

    actions = controller.determine_executor_actions()

    assert len(actions) == 1
    assert type(actions[0]).__name__ == "StopExecutorAction"
    assert actions[0].keep_position is True
    assert not any(type(action).__name__ == "CreateExecutorAction" for action in actions)
    assert controller._last_reconciliation["stops"] == 1
    assert controller._last_reconciliation["creates"] == 0


def test_armed_ready_controller_constructs_create_action_without_executing_it():
    config = DeriveOptionsAdaptiveGridConfig(
        execution_enabled=True,
        mainnet_armed=True,
        manual_kill_switch=False,
    )
    controller = DeriveOptionsAdaptiveGrid(config, FakeProvider(), None)
    controller._mode_decision = ModeDecision(
        GridMode.NORMAL,
        MarketState.NORMAL,
        True,
        True,
        True,
        True,
        1_700_000_000.0,
        (),
    )
    controller._grid_plan = build_grid_plan(
        center_price=100,
        mode=controller._mode_decision,
        inventory=InventorySnapshot(),
        rules=GridRules(),
    )
    controller._risk = RiskGateState(True, True, True, True, True, False, ())

    actions = controller.determine_executor_actions()

    assert len(actions) == 1
    assert type(actions[0]).__name__ == "CreateExecutorAction"
    assert actions[0].executor_config.keep_position is True
    assert actions[0].executor_config.triple_barrier_config.time_limit is None


def test_connector_readiness_is_an_explicit_fail_closed_gate():
    async def exercise():
        provider = FakeProvider()
        provider.connector.ready = False
        controller = DeriveOptionsAdaptiveGrid(DeriveOptionsAdaptiveGridConfig(), provider, None)
        controller.options_provider = FakeOptionsProvider(provider.now - 1)
        await controller.update_processed_data()
        info = controller.get_custom_info()
        assert info["connector_ready"] is False
        assert info["risk_gates"]["connector_ready"] is False
        assert "connector_not_ready" in info["risk_gates"]["reasons"]
        assert info["grid_mode"] == "NORMAL"

    asyncio.run(exercise())
