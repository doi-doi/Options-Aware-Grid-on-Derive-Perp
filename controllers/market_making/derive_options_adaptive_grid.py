# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""SOL options-IV adaptive grid controller for Hummingbot V2.

The controller is deliberately an adapter, not an independent execution
engine. Hummingbot owns all executor lifecycle and order submission. This
module only reads the installed Derive connector, reads the public SOL options
adapter, calculates a causal market state, and emits native GridExecutor
actions after every safety gate passes.

The default configuration is a continuously running, disarmed mainnet-shaped
shadow calculation: ``execution_enabled=False``, ``mainnet_armed=False``, and
``manual_kill_switch=False``. The latter is important because the installed
``v2_with_controllers.py`` treats ``manual_kill_switch=True`` as a generic
cash-out/stop command. The controller also enforces one active grid side
because Derive exposes ONEWAY positions. Connector close/post-only semantics
are separate hard gates and are never inferred from this controller.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass, replace
from decimal import ROUND_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from hummingbot.core.data_type.common import (
    MarketDict,
    OrderType,
    PositionMode,
    PriceType,
    TradeType,
)
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import (
    CreateExecutorAction,
    ExecutorAction,
    StopExecutorAction,
)
from pydantic import Field, model_validator

# Insert the support package before the controller directory. Hummingbot may
# load this file under its stem (`derive_options_adaptive_grid`), which would
# otherwise shadow the pure package that has the same repository-facing name.
_import_paths = (
    Path(__file__).resolve().parents[2] / "src",
    Path(__file__).resolve().parent / "derive_options_adaptive_grid_support",
)
for _source_path in reversed(_import_paths):
    if _source_path.exists() and str(_source_path) not in sys.path:
        sys.path.insert(0, str(_source_path))

# The source package is installed by scripts/install_controller.sh in Condor.
# The fallback keeps the controller directly importable from a clean checkout
# and makes the boundary explicit without bundling another order client.
try:
    from derive_options_adaptive_grid.capital import (
        CapitalPolicy,
        calculate_capital_snapshot,
        with_selected_leg,
    )
    from derive_options_adaptive_grid.connector_contract import (
        ConnectorContractSnapshot,
        account_binding_from_connector,
        inspect_connector_contract,
        normalized_fee_observation,
    )
    from derive_options_adaptive_grid.evidence import EvidenceLedger
    from derive_options_adaptive_grid.grid import GridRules, build_grid_plan
    from derive_options_adaptive_grid.models import (
        CapitalSnapshot,
        EvidenceKind,
        GridMode,
        GridPlan,
        InventorySnapshot,
        MarketState,
        ModeDecision,
        OptionsSnapshot,
        PerpSnapshot,
        RiskGateState,
        StateDecision,
    )
    from derive_options_adaptive_grid.modes import ModeConfig, determine_mode
    from derive_options_adaptive_grid.options_iv import (
        DeriveOptionsProvider,
        unavailable_options_snapshot,
    )
    from derive_options_adaptive_grid.research.models import CalibrationObservation
    from derive_options_adaptive_grid.state import CausalIVState, StateConfig
except ModuleNotFoundError:
    _support_root = Path(__file__).resolve().parent / "derive_options_adaptive_grid_support"
    _repo_src = Path(__file__).resolve().parents[2] / "src"
    _package_roots = (
        _support_root / "derive_options_adaptive_grid",
        _repo_src / "derive_options_adaptive_grid",
    )
    _package_root = next((path for path in _package_roots if path.exists()), None)
    if _package_root is None:
        raise
    _support_namespace = "_derive_options_adaptive_grid_support"
    _support_spec = importlib.util.spec_from_file_location(
        _support_namespace,
        _package_root / "__init__.py",
        submodule_search_locations=[str(_package_root)],
    )
    if _support_spec is None or _support_spec.loader is None:
        raise ModuleNotFoundError("unable to load the pure adaptive-grid support package") from None
    _support_module = importlib.util.module_from_spec(_support_spec)
    sys.modules[_support_namespace] = _support_module
    _support_spec.loader.exec_module(_support_module)
    from _derive_options_adaptive_grid_support.capital import (
        CapitalPolicy,
        calculate_capital_snapshot,
        with_selected_leg,
    )
    from _derive_options_adaptive_grid_support.connector_contract import (
        ConnectorContractSnapshot,
        account_binding_from_connector,
        inspect_connector_contract,
        normalized_fee_observation,
    )
    from _derive_options_adaptive_grid_support.evidence import EvidenceLedger
    from _derive_options_adaptive_grid_support.grid import GridRules, build_grid_plan
    from _derive_options_adaptive_grid_support.models import (
        CapitalSnapshot,
        EvidenceKind,
        GridMode,
        GridPlan,
        InventorySnapshot,
        MarketState,
        ModeDecision,
        OptionsSnapshot,
        PerpSnapshot,
        RiskGateState,
        StateDecision,
    )
    from _derive_options_adaptive_grid_support.modes import ModeConfig, determine_mode
    from _derive_options_adaptive_grid_support.options_iv import (
        DeriveOptionsProvider,
        unavailable_options_snapshot,
    )
    from _derive_options_adaptive_grid_support.research.models import CalibrationObservation
    from _derive_options_adaptive_grid_support.state import CausalIVState, StateConfig


ZERO = Decimal("0")
BUY_LEVEL_ID = "sol_grid_buy"
SELL_LEVEL_ID = "sol_grid_sell"
GRID_EXECUTOR_TYPE = "grid_executor"

def _cached_connector_contract(connector: Any | None = None) -> ConnectorContractSnapshot:
    """Load installed-runtime evidence bound to this controller's connector."""

    return inspect_connector_contract(connector)


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return default
    return result if result.is_finite() else default


def _optional_decimal(value: Any) -> Decimal | None:
    """Parse a finite Decimal without turning missing account data into zero."""

    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return result if result.is_finite() else None


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        raw_value = value.value
        return raw_value if isinstance(raw_value, str) else value.name
    return str(value)


def _wire(value: Any) -> Any:
    """Convert controller diagnostics to JSON-compatible values."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value if isinstance(value.value, str) else value.name
    if is_dataclass(value):
        return _wire(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_wire(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class DeriveOptionsAdaptiveGridConfig(ControllerConfigBase):
    """Strict mainnet SOL-USDC configuration for the native grid adapter."""

    id: str = "derive_options_adaptive_grid"
    controller_name: str = "derive_options_adaptive_grid"
    controller_type: str = "market_making"

    connector_name: str = "derive_perpetual"
    trading_pair: str = "SOL-USDC"
    exchange_instrument: str = "SOL-PERP"
    environment: str = "mainnet"
    position_mode: PositionMode = PositionMode.ONEWAY
    leverage: int = Field(default=1, ge=1, le=1)

    # No mutation can occur under these defaults. All three live gates are
    # checked again in determine_executor_actions rather than trusted here.
    mainnet_armed: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    execution_enabled: bool = False
    manual_kill_switch: bool = Field(default=False, json_schema_extra={"is_updatable": True})

    # Derive is ONEWAY in the installed connector. A strategy instance owns
    # one explicit side; a side change requires a new reviewed configuration.
    oneway_side: TradeType = TradeType.BUY
    max_active_executors: int = Field(default=1, ge=1, le=1)
    total_amount_quote: Decimal = Field(default=Decimal("100"), gt=0)
    defensive_total_quote: Decimal = Field(default=Decimal("60"), gt=0)
    min_order_amount_quote: Decimal = Field(default=Decimal("5"), gt=0)
    min_quote_increment: Decimal = Field(default=Decimal("0.01"), gt=0)
    max_position_quote: Decimal = Field(default=Decimal("50"), gt=0)
    hard_position_quote: Decimal = Field(default=Decimal("75"), gt=0)
    minimum_available_collateral_quote: Decimal = Field(default=Decimal("100"), gt=0)
    max_order_notional_quote: Decimal = Field(default=Decimal("100"), gt=0)

    # Dynamic capital is the live sizing policy. The legacy collateral floor is
    # retained only for an explicitly selected STATIC policy.
    capital_allocation_mode: Literal["DYNAMIC_AVAILABLE", "STATIC"] = "DYNAMIC_AVAILABLE"
    capital_reserve_pct: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    capital_reserve_quote: Decimal = Field(default=Decimal("20"), ge=0)
    capital_utilization_pct: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    capital_fee_buffer_pct: Decimal = Field(default=Decimal("0.02"), ge=0)
    capital_fee_buffer_quote: Decimal = Field(default=Decimal("5"), ge=0)
    hard_position_multiplier: Decimal = Field(default=Decimal("1.25"), ge=1)
    min_profit_buffer_bps: Decimal = Field(default=Decimal("2"), ge=0)

    # These flags are evidence inputs, not permissions. They stay false until
    # the installed connector contract probe proves both wire semantics.
    connector_close_semantics_verified: bool = False
    connector_post_only_semantics_verified: bool = False
    connector_lifecycle_verified: bool = False

    aggressive_enter_ratio: float = Field(default=0.85, gt=0, le=1.0)
    aggressive_exit_ratio: float = Field(default=0.95, gt=0, le=1.0)
    aggressive_half_width_pct: Decimal = Field(default=Decimal("0.0075"), gt=0)
    aggressive_levels: int = Field(default=7, ge=1, le=20)
    aggressive_total_quote: Decimal = Field(default=Decimal("100"), gt=0)
    normal_half_width_pct: Decimal = Field(default=Decimal("0.010"), gt=0)
    defensive_half_width_pct: Decimal = Field(default=Decimal("0.025"), gt=0)
    normal_levels: int = Field(default=5, ge=1, le=20)
    defensive_levels: int = Field(default=3, ge=1, le=20)
    activation_bounds_pct: Decimal | None = Field(default=Decimal("0.025"), gt=0)
    order_frequency: int = Field(default=5, ge=0, le=3600)
    max_orders_per_batch: int = Field(default=1, ge=1, le=20)
    min_spread_between_orders: Decimal = Field(default=Decimal("0.0005"), ge=0)
    safe_extra_spread: Decimal = Field(default=Decimal("0.0001"), ge=0)
    take_profit_pct: Decimal = Field(default=Decimal("0.001"), gt=0)
    # A native GridExecutor timeout would submit a MARKET close through the
    # current non-reduce-only Derive connector. Leave autonomous timeout off;
    # state-change reconciliation remains the reviewed stop boundary.
    time_limit_seconds: int | None = Field(default=None, ge=1)

    max_perp_age_seconds: float = Field(default=10.0, gt=0, le=300)
    max_option_data_age_seconds: float = Field(default=15.0, gt=0, le=300)
    future_tolerance_seconds: float = Field(default=2.0, ge=0, le=30)
    options_refresh_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    state_min_history: int = Field(default=5, ge=2, le=120)
    state_max_history: int = Field(default=120, ge=5, le=1000)
    high_enter_ratio: float = Field(default=1.25, ge=1.0)
    high_exit_ratio: float = Field(default=1.12, ge=1.0)
    extreme_enter_ratio: float = Field(default=1.60, ge=1.0)
    extreme_exit_ratio: float = Field(default=1.35, ge=1.0)
    price_tolerance_bps: Decimal = Field(default=Decimal("25"), ge=0, le=5000)
    amount_tolerance_pct: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    max_executor_age_seconds: float = Field(default=1800.0, gt=0)
    evidence_max_records: int = Field(default=200, ge=20, le=2000)

    @model_validator(mode="after")
    def validate_target(self) -> DeriveOptionsAdaptiveGridConfig:
        asserted_connector_proofs = tuple(
            name
            for name in (
                "connector_close_semantics_verified",
                "connector_post_only_semantics_verified",
                "connector_lifecycle_verified",
            )
            if getattr(self, name)
        )
        if asserted_connector_proofs:
            raise ValueError(
                "connector proof flags are runtime evidence only and must remain false: "
                + ", ".join(asserted_connector_proofs)
            )
        if self.connector_name != "derive_perpetual":
            raise ValueError("connector_name must be derive_perpetual")
        if self.trading_pair != "SOL-USDC":
            raise ValueError("trading_pair must be SOL-USDC")
        if self.exchange_instrument != "SOL-PERP":
            raise ValueError("exchange_instrument must be SOL-PERP")
        if self.environment.lower() != "mainnet":
            raise ValueError("environment must be mainnet")
        if self.position_mode != PositionMode.ONEWAY:
            raise ValueError("Derive supports only PositionMode.ONEWAY")
        if self.leverage != 1:
            raise ValueError("leverage is fixed at 1 for this controller")
        if self.max_active_executors != 1:
            raise ValueError("one-way policy permits exactly one active executor")
        if self.defensive_total_quote > self.total_amount_quote:
            raise ValueError("defensive_total_quote cannot exceed total_amount_quote")
        if self.aggressive_total_quote > self.total_amount_quote:
            raise ValueError("aggressive_total_quote cannot exceed total_amount_quote")
        if self.max_order_notional_quote > self.total_amount_quote:
            raise ValueError("max_order_notional_quote cannot exceed total_amount_quote")
        if self.hard_position_quote < self.max_position_quote:
            raise ValueError("hard_position_quote must be at least max_position_quote")
        if not (
            0
            < self.aggressive_enter_ratio
            < self.aggressive_exit_ratio
            <= 1.0
            <= self.high_exit_ratio
            < self.high_enter_ratio
            <= self.extreme_exit_ratio
            < self.extreme_enter_ratio
        ):
            raise ValueError(
                "state ratios must satisfy "
                "0 < aggressive_enter_ratio < aggressive_exit_ratio <= 1.0 <= "
                "high_exit_ratio < high_enter_ratio <= "
                "extreme_exit_ratio < extreme_enter_ratio"
            )
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class DeriveOptionsAdaptiveGrid(ControllerBase):
    """Causal options-IV state machine that emits native grid actions only."""

    config: DeriveOptionsAdaptiveGridConfig

    def __init__(
        self,
        config: DeriveOptionsAdaptiveGridConfig,
        market_data_provider,
        actions_queue,
        update_interval: float = 1.0,
    ):
        super().__init__(config, market_data_provider, actions_queue, update_interval)
        self.config = config
        self.options_provider = DeriveOptionsProvider(
            currency="SOL",
            environment="mainnet",
            max_option_data_age_seconds=config.max_option_data_age_seconds,
            future_tolerance_seconds=config.future_tolerance_seconds,
        )
        self.iv_state = CausalIVState(
            StateConfig(
                min_history=config.state_min_history,
                max_history=config.state_max_history,
                aggressive_enter_ratio=config.aggressive_enter_ratio,
                aggressive_exit_ratio=config.aggressive_exit_ratio,
                high_enter_ratio=config.high_enter_ratio,
                high_exit_ratio=config.high_exit_ratio,
                extreme_enter_ratio=config.extreme_enter_ratio,
                extreme_exit_ratio=config.extreme_exit_ratio,
                max_option_age_seconds=config.max_option_data_age_seconds,
                future_tolerance_seconds=config.future_tolerance_seconds,
            )
        )
        self.evidence = EvidenceLedger(max_records=config.evidence_max_records)
        self._last_book_marker: tuple[Any, Any] | None = None
        self._last_book_observed_at: float | None = None
        self._last_options_fetch_at: float | None = None
        self._last_options_source_timestamp: float | None = None
        self._seen_fill_evidence: set[str] = set()
        self._options_snapshot = unavailable_options_snapshot(
            now=self._now(), reference_price=None, errors=("options_not_sampled",)
        )
        self._perp_snapshot: PerpSnapshot | None = None
        self._calibration_observation: dict[str, Any] | None = None
        self._calibration_observation_ready = False
        self._calibration_observation_errors: tuple[str, ...] = (
            "calibration_observation_not_sampled",
        )
        self._inventory = InventorySnapshot(errors=("inventory_not_sampled",))
        self._capital_snapshot = CapitalSnapshot(errors=("capital_not_sampled",))
        self._connector_contract = _cached_connector_contract(self._connector())
        self._fee_economics: dict[str, Any] = {
            "ready": False,
            "source": "not_sampled",
            "fee_model_status": "UNKNOWN",
            "fee_source_mismatch": False,
            "raw_hummingbot_maker_fee": None,
            "raw_hummingbot_taker_fee": None,
            "raw_hummingbot_fee_unit": "decimal_fraction",
            "schema_maker_fee_unit": "decimal_fraction",
            "schema_taker_fee_unit": "decimal_fraction",
            "normalized_maker_fee_decimal": None,
            "normalized_taker_fee_decimal": None,
            "derive_metadata_maker_fee_decimal": None,
            "derive_metadata_taker_fee_decimal": None,
            "maker_fee_pct": None,
            "taker_fee_pct": None,
            "schema_maker_fee_pct": None,
            "schema_taker_fee_pct": None,
            "instrument_metadata_maker_fee_pct": None,
            "instrument_metadata_taker_fee_pct": None,
            "effective_fee_basis": None,
            "normal_round_trip_fee_pct": None,
            "emergency_round_trip_fee_pct": None,
            "schema_normal_round_trip_fee_pct": None,
            "schema_emergency_round_trip_fee_pct": None,
            "metadata_normal_round_trip_fee_pct": None,
            "metadata_emergency_round_trip_fee_pct": None,
            "normal_round_trip_fee_decimal": None,
            "emergency_round_trip_fee_decimal": None,
            "min_profit_buffer_bps": config.min_profit_buffer_bps,
            "minimum_economic_take_profit_pct": None,
            "configured_take_profit_pct": config.take_profit_pct,
            "warnings": (),
            "errors": ("fee_economics_not_sampled",),
        }
        self._risk = RiskGateState(
            reasons=("controller_not_sampled",),
            connector_ready=False,
            grid_size_ready=False,
            capital_ready=False,
            fee_economics_ready=False,
            entry_semantics_ready=False,
            exit_semantics_ready=False,
            lifecycle_ready=False,
        )
        self._state_decision = StateDecision(
            state=MarketState.INITIALIZING,
            current_iv=None,
            baseline_iv=None,
            iv_ratio=None,
            history_size=0,
            accepted=False,
            decision_timestamp=self._now(),
            reasons=("controller_not_sampled",),
        )
        self._mode_decision = ModeDecision(
            mode=GridMode.NORMAL,
            state=MarketState.INITIALIZING,
            perp_market_ready=False,
            options_data_available=False,
            buy_allowed=False,
            sell_allowed=False,
            decision_timestamp=self._now(),
            reasons=("controller_not_sampled",),
        )
        self._grid_plan = GridPlan(
            plan_version="uninitialized",
            mode=GridMode.NORMAL,
            center_price=ZERO,
            decision_timestamp=self._now(),
            legs=(),
            valid=False,
            reason="controller_not_sampled",
            errors=("controller_not_sampled",),
        )
        self._trading_rule: Any | None = None
        self._projected_headroom_quote = ZERO
        self._minimum_grid_order_quote = ZERO
        self._grid_size_block_reason: str | None = "grid_not_sampled"
        self._last_reconciliation = {
            "desired": 0,
            "active": 0,
            "pending": 0,
            "kept": 0,
            "stops": 0,
            "creates": 0,
            "replacements": 0,
            "duplicates": 0,
            "unmanaged": 0,
            "last_actions": [],
        }
        self._last_errors: list[str] = []
        try:
            self.market_data_provider.initialize_rate_sources(
                [
                    ConnectorPair(
                        connector_name=config.connector_name,
                        trading_pair=config.trading_pair,
                    )
                ]
            )
        except Exception as exc:
            self._last_errors.append(f"rate_source_initialization:{type(exc).__name__}")

    def _now(self) -> float:
        try:
            return float(self.market_data_provider.time())
        except (AttributeError, TypeError, ValueError):
            return time.time()

    def _connector(self) -> Any:
        return self.market_data_provider.get_connector(self.config.connector_name)

    def _read_perp_snapshot(self, now: float) -> PerpSnapshot:
        errors: list[str] = []
        pricing_errors: list[str] = []
        bid = ask = ZERO
        provider_bid = provider_ask = None
        marker: tuple[Any, Any] | None = None
        price_increment = _decimal(getattr(self._trading_rule, "min_price_increment", None))

        def top_price(order_book: Any, side: str) -> Decimal | None:
            entries = getattr(order_book, f"{side}_entries", None)
            if not callable(entries):
                return None
            try:
                row = next(iter(entries()), None)
            except Exception:
                return None
            return None if row is None else _decimal(getattr(row, "price", None))

        try:
            connector = self._connector()
            order_book = connector.get_order_book(self.config.trading_pair)
            bid = top_price(order_book, "bid")
            ask = top_price(order_book, "ask")
            if bid is None or ask is None:
                pricing_errors.append("order_book_top_of_book_unavailable")
                bid = bid or ZERO
                ask = ask or ZERO
            provider_bid = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.BestBid
                )
            )
            provider_ask = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.BestAsk
                )
            )
            if provider_bid <= 0 or provider_ask <= 0:
                pricing_errors.append("market_data_provider_bbo_unavailable")
            snapshot_uid = getattr(order_book, "snapshot_uid", None)
            last_update_id = getattr(order_book, "last_update_id", None)
            if snapshot_uid is None and last_update_id is None:
                pricing_errors.append("order_book_update_marker_unavailable")
            else:
                marker = (snapshot_uid, last_update_id)
                if marker != self._last_book_marker:
                    self._last_book_marker = marker
                    self._last_book_observed_at = now
        except Exception as exc:
            pricing_errors.append(f"perpetual_read_error:{type(exc).__name__}")

        if bid <= 0 or ask <= 0:
            pricing_errors.append("best_bid_ask_unavailable")
        elif ask <= bid:
            pricing_errors.append("best_ask_not_above_best_bid")
        if (
            provider_bid is not None
            and provider_ask is not None
            and provider_bid > 0
            and provider_ask > 0
        ):
            crosscheck_delta = max(abs(bid - provider_bid), abs(ask - provider_ask))
            allowed_delta = price_increment if price_increment > 0 else ZERO
            if crosscheck_delta > allowed_delta:
                pricing_errors.append("BBO_SOURCE_MISMATCH")
        else:
            crosscheck_delta = None
        if self._last_book_observed_at is None:
            errors.append("order_book_has_not_observed_an_update")
            source_timestamp = now
        else:
            source_timestamp = self._last_book_observed_at
            age = now - source_timestamp
            if age < -self.config.future_tolerance_seconds:
                errors.append("order_book_observation_in_future")
            elif age > self.config.max_perp_age_seconds:
                errors.append("order_book_observation_stale")
        if pricing_errors:
            errors.extend(pricing_errors)
        snapshot = PerpSnapshot(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            exchange_instrument=self.config.exchange_instrument,
            bid=bid,
            ask=ask,
            source_timestamp=source_timestamp,
            received_timestamp=now,
            decision_timestamp=now,
            source="hummingbot_order_book_observed_update_clock",
            errors=tuple(dict.fromkeys(errors)),
            order_book_marker=marker,
            price_increment=price_increment if price_increment > 0 else None,
            market_data_provider_bid=provider_bid,
            market_data_provider_ask=provider_ask,
            bbo_crosscheck_delta=crosscheck_delta,
            pricing_ready=not pricing_errors,
            pricing_errors=tuple(dict.fromkeys(pricing_errors)),
        )
        self.evidence.add(
            EvidenceKind.PUBLIC_MARKET_DATA,
            timestamp=now,
            source=snapshot.source,
            bid=bid,
            ask=ask,
            marker=_wire(marker),
            errors=snapshot.errors,
        )
        return snapshot

    def _read_trading_rule(self) -> Any | None:
        try:
            rule = self.market_data_provider.get_trading_rules(
                self.config.connector_name, self.config.trading_pair
            )
        except Exception as exc:
            self._last_errors.append(f"trading_rule_error:{type(exc).__name__}")
            return None
        if rule is None:
            self._last_errors.append("trading_rule_missing")
            return None
        increment = _decimal(getattr(rule, "min_price_increment", None))
        if increment <= 0:
            self._last_errors.append("trading_rule_price_increment_missing")
            return None
        return rule

    def _grid_rules(self, midpoint: Decimal) -> GridRules | None:
        rule = self._trading_rule
        if rule is None:
            self._projected_headroom_quote = ZERO
            self._minimum_grid_order_quote = ZERO
            self._grid_size_block_reason = "trading_rule_unavailable"
            return None
        price_increment = _decimal(getattr(rule, "min_price_increment", None))
        quote_increment = _decimal(
            getattr(rule, "min_quote_amount_increment", None), self.config.min_quote_increment
        )
        if quote_increment <= 0:
            quote_increment = self.config.min_quote_increment
        min_notional = _decimal(getattr(rule, "min_notional_size", None))
        # ``min_order_size`` is the connector's executable base-amount floor;
        # ``min_base_amount_increment`` is only the quantity tick.  They are
        # different on Derive (for SOL-USDC, 0.1 versus 0.001), so using the
        # increment as the minimum would understate the native order floor.
        min_order_size = _decimal(getattr(rule, "min_order_size", None))
        base_amount_increment = _decimal(getattr(rule, "min_base_amount_increment", None))
        quantized_min_base = min_order_size
        if quantized_min_base > 0 and base_amount_increment > 0:
            quantized_min_base = (quantized_min_base / base_amount_increment).to_integral_value(
                rounding=ROUND_UP
            ) * base_amount_increment
        max_half_width = max(
            self.config.aggressive_half_width_pct,
            self.config.normal_half_width_pct,
            self.config.defensive_half_width_pct,
        )
        conservative_max_price = midpoint * (Decimal("1") + max_half_width)
        min_order = max(
            self.config.min_order_amount_quote,
            min_notional,
            quantized_min_base * conservative_max_price,
        )
        if quote_increment > 0:
            min_order = (min_order / quote_increment).to_integral_value(
                rounding=ROUND_UP
            ) * quote_increment
        # A grid executor can fill every level. Size the complete grid against
        # remaining same-side soft and hard headroom, not only current
        # position, so a one-way side cannot overshoot either cap on a full
        # fill.
        position_quote = _decimal(self._inventory.position_quote)
        same_side_position = (
            max(position_quote, ZERO)
            if self.config.oneway_side == TradeType.BUY
            else max(-position_quote, ZERO)
        )
        soft_headroom = max(ZERO, self._inventory.max_position_quote - same_side_position)
        hard_headroom = max(
            ZERO,
            self._inventory.hard_position_quote - _decimal(abs(self._inventory.position_quote)),
        )
        projected_headroom = min(soft_headroom, hard_headroom)
        self._projected_headroom_quote = projected_headroom
        self._minimum_grid_order_quote = min_order
        self._grid_size_block_reason = (
            None
            if projected_headroom >= min_order
            else "projected_inventory_headroom_below_minimum_grid_order"
        )
        bbo = self._perp_snapshot
        return GridRules(
            aggressive_half_width_pct=self.config.aggressive_half_width_pct,
            normal_half_width_pct=self.config.normal_half_width_pct,
            defensive_half_width_pct=self.config.defensive_half_width_pct,
            aggressive_levels=self.config.aggressive_levels,
            normal_levels=self.config.normal_levels,
            defensive_levels=self.config.defensive_levels,
            aggressive_total_quote=min(
                self.config.aggressive_total_quote,
                self._capital_snapshot.aggressive_budget_quote,
                projected_headroom,
            ),
            normal_total_quote=min(
                self.config.total_amount_quote,
                self._capital_snapshot.normal_budget_quote,
                projected_headroom,
            ),
            defensive_total_quote=min(
                self.config.defensive_total_quote,
                self._capital_snapshot.defensive_budget_quote,
                projected_headroom,
            ),
            min_order_amount_quote=self.config.min_order_amount_quote,
            min_order_size_base=min_order_size,
            base_amount_increment=base_amount_increment,
            min_notional_quote=min_notional,
            price_increment=price_increment,
            quote_increment=quote_increment,
            activation_bounds_pct=self.config.activation_bounds_pct,
            order_frequency=self.config.order_frequency,
            max_orders_per_batch=self.config.max_orders_per_batch,
            min_spread_between_orders=self.config.min_spread_between_orders,
            best_bid=ZERO if bbo is None else bbo.bid,
            best_ask=ZERO if bbo is None else bbo.ask,
            safe_extra_spread=self.config.safe_extra_spread,
        )

    @staticmethod
    def _position_signed_amount(position: Any) -> Decimal:
        amount = _decimal(getattr(position, "amount", None))
        side = _enum_value(getattr(position, "position_side", ""))
        if side.upper().endswith("SHORT"):
            return -abs(amount)
        if side.upper().endswith("LONG"):
            return abs(amount)
        return amount

    def _read_inventory(self, midpoint: Decimal, now: float) -> InventorySnapshot:
        errors: list[str] = []
        position_base = ZERO
        collateral: Decimal | None = None
        try:
            connector = self._connector()
            positions = getattr(connector, "account_positions", None)
            if positions is None:
                errors.append("account_positions_unavailable")
            elif not isinstance(positions, Mapping):
                errors.append("account_positions_unreadable")
            else:
                for pair, position in positions.items():
                    if getattr(position, "trading_pair", pair) != self.config.trading_pair:
                        continue
                    position_base += self._position_signed_amount(position)
            try:
                collateral = _optional_decimal(connector.get_available_balance("USDC"))
            except Exception:
                collateral = None
            if collateral is None:
                # The connector method is authoritative when it returns a
                # valid value. Only fall back to a valid USDC entry in the
                # connector's available-balance mapping when that method is
                # unavailable or raises.
                available = getattr(connector, "available_balances", {})
                if isinstance(available, Mapping):
                    collateral = _optional_decimal(available.get("USDC"))
            if collateral is None:
                errors.append("available_USDC_collateral_unavailable")
        except Exception as exc:
            errors.append(f"inventory_read_error:{type(exc).__name__}")
        position_quote = position_base * midpoint
        return InventorySnapshot(
            position_base=position_base,
            position_quote=position_quote,
            available_collateral_quote=collateral,
            max_position_quote=self.config.max_position_quote,
            hard_position_quote=self.config.hard_position_quote,
            source_timestamp=now,
            received_timestamp=now,
            decision_timestamp=now,
            source="hummingbot_derive_account",
            errors=tuple(dict.fromkeys(errors)),
        )

    def _options_due(self, now: float) -> bool:
        return (
            self._last_options_fetch_at is None
            or now - self._last_options_fetch_at >= self.config.options_refresh_interval_seconds
        )

    def _calibration_input_errors(
        self,
        perp: PerpSnapshot | None,
        options: OptionsSnapshot,
        decision_timestamp: float,
    ) -> tuple[str, ...]:
        """Return only errors that make the current capture inputs unusable."""

        errors: list[str] = []
        if perp is None:
            errors.append("perp_not_ready")
        elif not perp.market_ready:
            errors.extend(perp.errors or ("perp_not_ready",))
        if perp is not None:
            errors.extend(perp.pricing_errors)
        if not options.option_data_available:
            errors.extend(options.errors or ("options_not_ready",))
        if options.underlying != "SOL":
            errors.append("options_underlying_must_be_SOL")
        if options.environment.lower() != "mainnet":
            errors.append("options_environment_must_be_mainnet")
        source_timestamp = _finite_float(options.source_timestamp)
        received_timestamp = _finite_float(options.received_timestamp)
        if source_timestamp is None:
            errors.append("options_source_timestamp_missing")
        if received_timestamp is None:
            errors.append("options_received_timestamp_missing")
        if source_timestamp is not None and received_timestamp is not None:
            if source_timestamp > received_timestamp + self.config.future_tolerance_seconds:
                errors.append("options_source_after_receipt")
            if received_timestamp > decision_timestamp + self.config.future_tolerance_seconds:
                errors.append("options_receipt_after_decision")
            if source_timestamp > decision_timestamp + self.config.future_tolerance_seconds:
                errors.append("options_source_after_decision")
            if decision_timestamp - source_timestamp > self.config.max_option_data_age_seconds:
                errors.append("options_observation_stale")
        if perp is not None:
            for name, value in (
                ("perp_source_timestamp", perp.source_timestamp),
                ("perp_received_timestamp", perp.received_timestamp),
                ("perp_decision_timestamp", perp.decision_timestamp),
            ):
                if _finite_float(value) is None:
                    errors.append(f"{name}_invalid")
            if options.reference_price is None:
                errors.append("option_reference_price_missing")
            else:
                option_reference = _decimal(options.reference_price)
                if abs(option_reference - perp.mid) > Decimal("0.000000000001"):
                    errors.append("option_reference_price_not_bbo_midpoint")
        return tuple(dict.fromkeys(errors))

    def _build_calibration_observation(
        self,
        perp: PerpSnapshot | None,
        options: OptionsSnapshot,
        decision_timestamp: float,
    ) -> tuple[bool, dict[str, Any] | None, tuple[str, ...]]:
        """Build one validator-backed row from the accepted decision cycle."""

        input_errors = self._calibration_input_errors(perp, options, decision_timestamp)
        if input_errors or perp is None:
            return False, None, input_errors or ("perp_not_ready",)
        payload = {
            "decision_timestamp": decision_timestamp,
            "source_timestamp": options.source_timestamp,
            "received_timestamp": options.received_timestamp,
            "underlying": "SOL",
            "trading_pair": self.config.trading_pair,
            "exchange_instrument": self.config.exchange_instrument,
            "environment": self.config.environment,
            # Decimal is retained through the executable/controller path. The
            # calibration schema is a JSON/float boundary only.
            "perp_mid": float(perp.mid),
            "best_bid": float(perp.bid),
            "best_ask": float(perp.ask),
            "atm_call_iv": options.call_iv,
            "atm_put_iv": options.put_iv,
            "atm_iv": options.atm_iv,
            "expiry_timestamp": options.expiry_timestamp,
            "days_to_expiry": options.days_to_expiry,
            "atm_strike": options.atm_strike,
            "call_strike": options.call_strike,
            "put_strike": options.put_strike,
            "call_instrument": options.call_instrument,
            "put_instrument": options.put_instrument,
            "call_iv_source": options.call_iv_source,
            "put_iv_source": options.put_iv_source,
            "option_reference_price": float(perp.mid),
            "source": "derive_options_adaptive_grid_controller",
            "evidence": EvidenceKind.SHADOW_PLAN.value,
        }
        try:
            observation = CalibrationObservation.from_mapping(payload)
        except (TypeError, ValueError) as exc:
            return False, None, (f"calibration_observation_parse:{type(exc).__name__}",)
        validation_errors = observation.validation_errors()
        if validation_errors:
            return False, None, validation_errors
        row = observation.to_dict()
        # Shadow rows must not imply an order.  The schema defaults this field
        # to None, but the persisted capture deliberately omits it.
        row.pop("native_order_id", None)
        return True, row, ()

    def _read_fee_economics(self) -> dict[str, Any]:
        """Compare decimal fee rates from the installed runtime and Derive.

        ``TradeFeeSchema.maker_percent_fee_decimal`` is already a decimal
        fraction.  The installed source proves that percentage-form YAML
        overrides are divided by 100, while Derive's connector defaults are
        passed directly into ``TradeFeeSchema``.  This method therefore never
        divides a schema value heuristically; the derived connector image
        owns the corrected decimal defaults and this method compares decimal
        rate to decimal rate.
        """

        errors: list[str] = []
        warnings: list[str] = []
        maker_fee = taker_fee = None
        instrument_maker_fee = instrument_taker_fee = None
        source = "hummingbot_trade_fee_schema"
        read_error: str | None = None
        maker_observation = normalized_fee_observation(None)
        taker_observation = normalized_fee_observation(None)
        try:
            connector = self._connector()
            instruments = getattr(connector, "_instrument_ticker", ())
            instrument = next(
                (
                    item
                    for item in instruments
                    if isinstance(item, Mapping)
                    and str(item.get("instrument_name", "")) == self.config.exchange_instrument
                ),
                None,
            )
            if instrument is None:
                warnings.append("derive_instrument_fee_metadata_unavailable")
            else:
                instrument_maker_fee = _optional_decimal(instrument.get("maker_fee_rate"))
                instrument_taker_fee = _optional_decimal(instrument.get("taker_fee_rate"))
                if instrument_maker_fee is None or instrument_taker_fee is None:
                    warnings.append("derive_instrument_fee_metadata_incomplete")
                if instrument_maker_fee is not None and instrument_maker_fee < ZERO:
                    instrument_maker_fee = ZERO
                if instrument_taker_fee is not None and instrument_taker_fee < ZERO:
                    instrument_taker_fee = ZERO

            from hummingbot.core.utils.estimate_fee import TradeFeeSchemaLoader

            schema = TradeFeeSchemaLoader.configured_schema_for_exchange(
                self.config.connector_name
            )
            maker_observation = normalized_fee_observation(schema.maker_percent_fee_decimal)
            taker_observation = normalized_fee_observation(schema.taker_percent_fee_decimal)
            maker_fee = _optional_decimal(maker_observation["normalized_decimal_rate"])
            taker_fee = _optional_decimal(taker_observation["normalized_decimal_rate"])
        except Exception as exc:
            source = "fee_metadata_read_error"
            read_error = type(exc).__name__

        schema_normal_round_trip = None if maker_fee is None else maker_fee + maker_fee
        schema_emergency_round_trip = (
            None if maker_fee is None or taker_fee is None else maker_fee + taker_fee
        )
        metadata_normal_round_trip = (
            None
            if instrument_maker_fee is None
            else instrument_maker_fee + instrument_maker_fee
        )
        metadata_emergency_round_trip = (
            None
            if instrument_maker_fee is None or instrument_taker_fee is None
            else instrument_maker_fee + instrument_taker_fee
        )

        if maker_fee is None or maker_fee < ZERO:
            errors.append("maker_fee_model_unavailable")
        if taker_fee is None or taker_fee < ZERO:
            errors.append("taker_fee_model_unavailable")
        if instrument_maker_fee is None or instrument_maker_fee < ZERO:
            errors.append("instrument_metadata_maker_fee_unavailable")
        if instrument_taker_fee is None or instrument_taker_fee < ZERO:
            errors.append("instrument_metadata_taker_fee_unavailable")
        if read_error is not None:
            errors.extend(("BLOCKED_BY_FEE_MODEL", f"fee_model_read_error:{read_error}"))

        material_mismatch = bool(
            maker_fee is not None
            and instrument_maker_fee is not None
            and taker_fee is not None
            and instrument_taker_fee is not None
            and (
                abs(maker_fee - instrument_maker_fee) > Decimal("0.000000000001")
                or abs(taker_fee - instrument_taker_fee) > Decimal("0.000000000001")
            )
        )
        if material_mismatch:
            warnings.append("runtime_fee_schema_differs_from_instrument_metadata")
            errors.extend(("BLOCKED_BY_FEE_MODEL", "fee_source_mismatch"))

        common = {
            "source": source,
            "raw_hummingbot_maker_fee": maker_observation["raw_value"],
            "raw_hummingbot_taker_fee": taker_observation["raw_value"],
            "raw_hummingbot_fee_unit": "decimal_fraction",
            "schema_maker_fee_unit": maker_observation["raw_unit"],
            "schema_taker_fee_unit": taker_observation["raw_unit"],
            "normalized_maker_fee_decimal": maker_fee,
            "normalized_taker_fee_decimal": taker_fee,
            "derive_metadata_maker_fee_decimal": instrument_maker_fee,
            "derive_metadata_taker_fee_decimal": instrument_taker_fee,
            "maker_fee_pct": maker_fee,
            "taker_fee_pct": taker_fee,
            "schema_maker_fee_pct": maker_fee,
            "schema_taker_fee_pct": taker_fee,
            "instrument_metadata_maker_fee_pct": instrument_maker_fee,
            "instrument_metadata_taker_fee_pct": instrument_taker_fee,
            "schema_normal_round_trip_fee_pct": schema_normal_round_trip,
            "schema_emergency_round_trip_fee_pct": schema_emergency_round_trip,
            "metadata_normal_round_trip_fee_pct": metadata_normal_round_trip,
            "metadata_emergency_round_trip_fee_pct": metadata_emergency_round_trip,
            "normal_round_trip_fee_decimal": schema_normal_round_trip,
            "emergency_round_trip_fee_decimal": schema_emergency_round_trip,
            "min_profit_buffer_bps": self.config.min_profit_buffer_bps,
            "configured_take_profit_pct": self.config.take_profit_pct,
            "warnings": tuple(dict.fromkeys(warnings)),
            "errors": tuple(dict.fromkeys(errors)),
        }
        if material_mismatch or errors:
            return {
                **common,
                "ready": False,
                "fee_model_status": "UNKNOWN",
                "fee_source_mismatch": material_mismatch,
                "effective_fee_basis": None,
                "normal_round_trip_fee_pct": None,
                "emergency_round_trip_fee_pct": None,
                "minimum_economic_take_profit_pct": None,
            }

        buffer_pct = self.config.min_profit_buffer_bps / Decimal("10000")
        minimum_take_profit = schema_normal_round_trip + buffer_pct
        if self.config.take_profit_pct <= minimum_take_profit:
            errors.extend(("BLOCKED_BY_FEE_MODEL", "take_profit_does_not_clear_fee_floor"))

        return {
            **common,
            "ready": not errors,
            "fee_model_status": "VERIFIED",
            "fee_source_mismatch": False,
            "effective_fee_basis": (
                "matching decimal Hummingbot schema and Derive instrument metadata"
            ),
            "normal_round_trip_fee_pct": schema_normal_round_trip,
            "emergency_round_trip_fee_pct": schema_emergency_round_trip,
            "minimum_economic_take_profit_pct": minimum_take_profit,
            "errors": tuple(dict.fromkeys(errors)),
        }

    def _calculate_capital_snapshot(self, available: Decimal | None, now: float) -> CapitalSnapshot:
        policy = CapitalPolicy(
            total_amount_quote=self.config.total_amount_quote,
            aggressive_total_quote=self.config.aggressive_total_quote,
            defensive_total_quote=self.config.defensive_total_quote,
            max_position_quote=self.config.max_position_quote,
            hard_position_quote=self.config.hard_position_quote,
            max_order_notional_quote=self.config.max_order_notional_quote,
            reserve_pct=self.config.capital_reserve_pct,
            reserve_quote=self.config.capital_reserve_quote,
            utilization_pct=self.config.capital_utilization_pct,
            fee_buffer_pct=self.config.capital_fee_buffer_pct,
            fee_buffer_quote=self.config.capital_fee_buffer_quote,
            hard_position_multiplier=self.config.hard_position_multiplier,
        )
        if self.config.capital_allocation_mode == "DYNAMIC_AVAILABLE":
            return calculate_capital_snapshot(
                available,
                policy,
                source_timestamp=now,
                decision_timestamp=now,
            )

        # STATIC is explicit backward-compatible behavior. It does not reuse
        # the dynamic reserve/utilization formula and still requires the
        # configured legacy balance floor and full strategy budget.
        parsed_available = _optional_decimal(available)
        errors: list[str] = []
        if parsed_available is None:
            errors.append("available_USDC_collateral_unavailable")
        else:
            if parsed_available < self.config.minimum_available_collateral_quote:
                errors.append("collateral_below_configured_floor")
            if parsed_available < self.config.total_amount_quote:
                errors.append("collateral_below_static_strategy_budget")
        target = self.config.total_amount_quote if not errors else ZERO
        deployable = parsed_available or ZERO
        soft = min(self.config.max_position_quote, target)
        hard = min(self.config.hard_position_quote, deployable)
        return CapitalSnapshot(
            available_collateral_quote=parsed_available,
            reserve_quote=ZERO,
            deployable_quote=deployable,
            target_strategy_quote=target,
            aggressive_budget_quote=min(self.config.aggressive_total_quote, target),
            normal_budget_quote=target,
            defensive_budget_quote=min(self.config.defensive_total_quote, target),
            dynamic_soft_position_quote=soft,
            dynamic_hard_position_quote=hard,
            fee_buffer_quote=ZERO,
            capital_ready=not errors,
            source_timestamp=now,
            decision_timestamp=now,
            errors=tuple(dict.fromkeys(errors)),
        )

    def _maker_price_safe(self, leg: Any | None) -> bool:
        if leg is None or self._perp_snapshot is None:
            return False
        if leg.side == "BUY":
            return all(price < self._perp_snapshot.ask for price in leg.expected_level_prices)
        if leg.side == "SELL":
            return all(price > self._perp_snapshot.bid for price in leg.expected_level_prices)
        return False

    def _native_minimum_for_leg(self, leg: Any | None) -> Decimal | None:
        """Recompute the connector floor at the selected leg's worst price."""

        if leg is None or self._trading_rule is None:
            return None
        rule = self._trading_rule
        min_order_size = _decimal(getattr(rule, "min_order_size", None))
        base_increment = _decimal(getattr(rule, "min_base_amount_increment", None))
        if min_order_size > 0 and base_increment > 0:
            min_order_size = (
                min_order_size / base_increment
            ).to_integral_value(rounding=ROUND_UP) * base_increment
        min_notional = _decimal(getattr(rule, "min_notional_size", None))
        quote_increment = _decimal(
            getattr(rule, "min_quote_amount_increment", None), self.config.min_quote_increment
        )
        if quote_increment <= 0:
            quote_increment = self.config.min_quote_increment
        native_minimum = max(
            self.config.min_order_amount_quote,
            min_notional,
            min_order_size * leg.end_price,
        )
        return (
            (native_minimum / quote_increment).to_integral_value(rounding=ROUND_UP)
            * quote_increment
            if quote_increment > 0
            else native_minimum
        )

    def _refresh_calibration_observation(
        self,
        *,
        perp: PerpSnapshot | None,
        options: OptionsSnapshot,
        decision_timestamp: float,
        new_options_source: bool,
    ) -> None:
        """Publish a current row, or fail closed without exposing stale data."""

        ready = False
        observation: dict[str, Any] | None = None
        errors: tuple[str, ...]
        if new_options_source:
            if self._state_decision.accepted:
                ready, observation, errors = self._build_calibration_observation(
                    perp, options, decision_timestamp
                )
            else:
                errors = tuple(
                    dict.fromkeys(
                        [
                            *self._calibration_input_errors(perp, options, decision_timestamp),
                            *(self._state_decision.reasons or ("iv_decision_not_accepted",)),
                        ]
                    )
                )
        elif self._calibration_observation is not None:
            errors_list = list(self._calibration_input_errors(perp, options, decision_timestamp))
            if options.source_timestamp != self._calibration_observation.get("source_timestamp"):
                errors_list.append("calibration_observation_source_changed")
            if not errors_list:
                try:
                    existing = CalibrationObservation.from_mapping(self._calibration_observation)
                    errors_list.extend(existing.validation_errors())
                except (TypeError, ValueError) as exc:
                    errors_list.append(f"calibration_observation_parse:{type(exc).__name__}")
            if errors_list:
                errors = tuple(dict.fromkeys(errors_list))
            else:
                ready = True
                observation = self._calibration_observation
                errors = ()
        else:
            errors = tuple(
                dict.fromkeys(
                    [
                        *self._calibration_input_errors(perp, options, decision_timestamp),
                        "calibration_observation_not_available",
                    ]
                )
            )
        self._calibration_observation_ready = ready
        self._calibration_observation = observation
        self._calibration_observation_errors = errors

    async def update_processed_data(self):
        """Refresh read-only inputs and publish the complete decision snapshot."""

        self._last_errors = []
        started_at = self._now()
        self._trading_rule = self._read_trading_rule()
        self._perp_snapshot = self._read_perp_snapshot(started_at)
        midpoint = (
            self._perp_snapshot.mid
            if self._perp_snapshot.bid > ZERO and self._perp_snapshot.ask > self._perp_snapshot.bid
            else ZERO
        )
        raw_inventory = self._read_inventory(midpoint, started_at)
        self._capital_snapshot = self._calculate_capital_snapshot(
            raw_inventory.available_collateral_quote,
            started_at,
        )
        self._inventory = replace(
            raw_inventory,
            max_position_quote=self._capital_snapshot.dynamic_soft_position_quote,
            hard_position_quote=self._capital_snapshot.dynamic_hard_position_quote,
        )
        rules = self._grid_rules(midpoint) if midpoint > 0 else None
        if rules is None and midpoint <= 0:
            self._projected_headroom_quote = ZERO
            self._minimum_grid_order_quote = ZERO
            self._grid_size_block_reason = "perpetual_midpoint_unavailable"

        if self._options_due(started_at):
            self._last_options_fetch_at = started_at
            try:
                self._options_snapshot = await self.options_provider.snapshot(
                    float(midpoint) if midpoint > ZERO else None
                )
            except Exception as exc:
                self._options_snapshot = unavailable_options_snapshot(
                    now=self._now(),
                    reference_price=(float(midpoint) if midpoint > ZERO else None),
                    errors=(f"options_provider_error:{type(exc).__name__}",),
                )

        decision_timestamp = max(started_at, self._options_snapshot.decision_timestamp, self._now())
        options = self._options_snapshot
        previous_options_source_timestamp = self._last_options_source_timestamp
        new_options_source = (
            options.option_data_available
            and options.source_timestamp is not None
            and options.source_timestamp != previous_options_source_timestamp
        )
        if (
            options.option_data_available
            and options.source_timestamp == previous_options_source_timestamp
        ):
            # Repeated ticker responses with the same source timestamp are a
            # refresh of one observation, not a new historical IV point.
            pass
        else:
            self._state_decision = self.iv_state.observe(
                options, decision_timestamp=decision_timestamp
            )
            if self._state_decision.accepted and options.source_timestamp is not None:
                self._last_options_source_timestamp = options.source_timestamp

        self._refresh_calibration_observation(
            perp=self._perp_snapshot,
            options=options,
            decision_timestamp=decision_timestamp,
            new_options_source=new_options_source,
        )

        market_ready = self._perp_snapshot.market_ready and not any(
            error in self._perp_snapshot.errors
            for error in ("order_book_observation_stale", "order_book_observation_in_future")
        )
        option_age = self._options_snapshot.option_data_age_seconds
        options_ready = bool(
            self._options_snapshot.option_data_available
            and self._options_snapshot.environment.lower() == "mainnet"
            and self._options_snapshot.underlying == "SOL"
            and option_age is not None
            and -self.config.future_tolerance_seconds
            <= option_age
            <= self.config.max_option_data_age_seconds
            and not self._options_snapshot.errors
        )
        state_ready = bool(
            self._state_decision.accepted and self._state_decision.state != MarketState.INITIALIZING
        )
        pricing_ready = bool(
            self._perp_snapshot
            and self._perp_snapshot.pricing_ready
            and not self._perp_snapshot.pricing_errors
        )

        # Classification is independent from execution authorization. The
        # current mode remains visible in shadow even while a hard gate is
        # false.
        self._mode_decision = determine_mode(
            state=self._state_decision.state,
            perp_market_ready=market_ready,
            options=options,
            inventory=self._inventory,
            risk=self._risk,
            decision_timestamp=decision_timestamp,
            config=ModeConfig(
                max_option_age_seconds=self.config.max_option_data_age_seconds,
                future_tolerance_seconds=self.config.future_tolerance_seconds,
            ),
        )
        if rules is None:
            self._grid_plan = GridPlan(
                plan_version="invalid",
                mode=self._mode_decision.mode,
                center_price=midpoint,
                decision_timestamp=decision_timestamp,
                legs=(),
                valid=False,
                reason="trading_rule_or_midpoint_unavailable",
                errors=("trading_rule_or_midpoint_unavailable",),
            )
        else:
            self._grid_plan = build_grid_plan(
                center_price=midpoint,
                mode=self._mode_decision,
                inventory=self._inventory,
                rules=rules,
            )

        desired_leg = self._desired_leg()
        self._capital_snapshot = with_selected_leg(
            self._capital_snapshot,
            selected_leg_total_quote=(
                ZERO if desired_leg is None else desired_leg.total_amount_quote
            ),
            selected_native_minimum_quote=(
                ZERO if desired_leg is None else desired_leg.min_order_amount_quote
            ),
            decision_timestamp=decision_timestamp,
        )

        active = self._active_grid_executors()
        unmanaged_active = self._has_unmanaged_active()
        try:
            connector_ready = bool(getattr(self._connector(), "ready", False))
        except Exception as exc:
            connector_ready = False
            self._last_errors.append(f"connector_ready_read_error:{type(exc).__name__}")
        contract = self._connector_contract
        incompatible_inventory = (
            self.config.oneway_side == TradeType.BUY and self._inventory.position_quote < 0
        ) or (self.config.oneway_side == TradeType.SELL and self._inventory.position_quote > 0)
        inventory_errors = list(self._inventory.errors)
        if incompatible_inventory:
            inventory_errors.append("existing_inventory_opposes_configured_oneway_side")
        dynamic_hard_exposure = abs(self._inventory.position_quote) > 0 and (
            self._inventory.hard_position_quote <= 0
            or abs(self._inventory.position_quote) >= self._inventory.hard_position_quote
        )
        if dynamic_hard_exposure:
            inventory_errors.append("dynamic_hard_position_limit_reached")
        inventory_ready = not inventory_errors
        collateral_ready = self._capital_snapshot.capital_ready
        executor_ready = len(active) <= self.config.max_active_executors and not unmanaged_active
        self._fee_economics = self._read_fee_economics()
        fee_economics_ready = bool(self._fee_economics.get("ready", False))
        maker_price_safe = self._maker_price_safe(desired_leg)
        entry_semantics_ready = bool(
            desired_leg is not None
            and maker_price_safe
            and contract.post_only_verified
        )
        exit_semantics_ready = bool(
            contract.close_contract_verified
            and contract.post_only_verified
            and self.config.time_limit_seconds is None
        )
        lifecycle_ready = contract.lifecycle_verified
        grid_size_ready = bool(
            rules is not None
            and self._grid_plan.valid
            and desired_leg is not None
            and self._grid_size_block_reason is None
        )
        risk_reasons: list[str] = [
            *self._perp_snapshot.errors,
            *self._options_snapshot.errors,
            *inventory_errors,
            *self._perp_snapshot.pricing_errors,
        ]
        if not options_ready and "options_not_ready" not in risk_reasons:
            risk_reasons.append("options_not_ready")
        if not state_ready and "iv_state_warming_up" not in risk_reasons:
            risk_reasons.append("iv_state_warming_up")
        if not pricing_ready and "pricing_not_ready" not in risk_reasons:
            risk_reasons.append("pricing_not_ready")
        if incompatible_inventory:
            risk_reasons.append("existing_inventory_opposes_configured_oneway_side")
        if dynamic_hard_exposure:
            risk_reasons.append("dynamic_hard_position_limit_reached")
        if not connector_ready:
            risk_reasons.append("connector_not_ready")
        if self._grid_size_block_reason:
            risk_reasons.append(self._grid_size_block_reason)
        if not grid_size_ready:
            risk_reasons.extend(self._grid_plan.errors or ("grid_plan_not_ready",))
        if self.config.manual_kill_switch:
            risk_reasons.append("manual_kill_switch_active")
        if not collateral_ready:
            risk_reasons.extend(self._capital_snapshot.errors or ("capital_not_ready",))
        if not executor_ready:
            risk_reasons.append("executor_cap_or_unmanaged_executor")
        if not fee_economics_ready:
            risk_reasons.extend(
                self._fee_economics.get("errors") or ("fee_economics_not_ready",)
            )
        if not entry_semantics_ready:
            risk_reasons.append("entry_semantics_not_verified")
            if not contract.post_only_verified:
                risk_reasons.append("BLOCKED_BY_POST_ONLY_SEMANTICS")
        if not exit_semantics_ready:
            risk_reasons.append("exit_semantics_not_verified")
            if not contract.close_contract_verified:
                risk_reasons.append("BLOCKED_BY_CONNECTOR_CLOSE_SEMANTICS")
        if not lifecycle_ready:
            risk_reasons.append("BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE")
        self._risk = RiskGateState(
            market_data_ready=market_ready,
            options_ready=options_ready,
            state_ready=state_ready,
            pricing_ready=pricing_ready,
            collateral_ready=collateral_ready,
            inventory_ready=inventory_ready,
            executor_ready=executor_ready,
            manual_kill_clear=not self.config.manual_kill_switch,
            connector_ready=connector_ready,
            grid_size_ready=grid_size_ready,
            capital_ready=self._capital_snapshot.capital_ready,
            fee_economics_ready=fee_economics_ready,
            entry_semantics_ready=entry_semantics_ready,
            exit_semantics_ready=exit_semantics_ready,
            lifecycle_ready=lifecycle_ready,
            hard_block=(
                dynamic_hard_exposure
                or incompatible_inventory
                or len(active) > self.config.max_active_executors
                or unmanaged_active
            ),
            reasons=tuple(dict.fromkeys(risk_reasons)),
        )
        self.processed_data = self.get_custom_info()

    def _strategy_executors(self) -> list[Any]:
        return [
            executor
            for executor in self.executors_info
            if getattr(executor, "type", None) == GRID_EXECUTOR_TYPE
            and getattr(executor, "controller_id", None) == self.config.id
        ]

    @staticmethod
    def _executor_id(executor: Any) -> str | None:
        value = getattr(executor, "id", None)
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _strategy_executor_ids(self) -> set[str]:
        return {
            executor_id
            for executor in self._strategy_executors()
            if (executor_id := self._executor_id(executor)) is not None
        }

    def _is_managed_active_executor(self, executor: Any, strategy_ids: set[str]) -> bool:
        # Active executor snapshots can be distinct Python objects from the
        # controller's executors_info records. Ownership therefore uses the
        # controller ID plus a stable native executor ID, never object identity.
        executor_id = self._executor_id(executor)
        return (
            getattr(executor, "type", None) == GRID_EXECUTOR_TYPE
            and getattr(executor, "controller_id", None) == self.config.id
            and executor_id is not None
            and executor_id in strategy_ids
        )

    def _active_grid_executors(self) -> list[Any]:
        return [
            executor
            for executor in self.get_active_executors(
                connector_names=[self.config.connector_name],
                trading_pairs=[self.config.trading_pair],
                executor_types=[GRID_EXECUTOR_TYPE],
            )
        ]

    def _has_unmanaged_active(self) -> bool:
        strategy_ids = self._strategy_executor_ids()
        return any(
            not self._is_managed_active_executor(executor, strategy_ids)
            for executor in self._active_grid_executors()
        )

    @staticmethod
    def _executor_level_id(executor: Any) -> str | None:
        return getattr(getattr(executor, "config", None), "level_id", None)

    def _desired_leg(self):
        if not self._grid_plan.valid:
            return None
        desired_side = self.config.oneway_side.name
        if desired_side == "BUY" and not self._mode_decision.buy_allowed:
            return None
        if desired_side == "SELL" and not self._mode_decision.sell_allowed:
            return None
        return next((leg for leg in self._grid_plan.legs if leg.side == desired_side), None)

    def _native_config(self, leg, timestamp: float) -> GridExecutorConfig:
        level_id = BUY_LEVEL_ID if leg.side == "BUY" else SELL_LEVEL_ID
        side = TradeType.BUY if leg.side == "BUY" else TradeType.SELL
        return GridExecutorConfig(
            timestamp=timestamp,
            controller_id=self.config.id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            start_price=leg.start_price,
            end_price=leg.end_price,
            limit_price=leg.limit_price,
            side=side,
            total_amount_quote=min(leg.total_amount_quote, self.config.max_order_notional_quote),
            min_spread_between_orders=leg.min_spread_between_orders,
            min_order_amount_quote=leg.min_order_amount_quote,
            max_open_orders=leg.max_open_orders,
            max_orders_per_batch=leg.max_orders_per_batch,
            order_frequency=leg.order_frequency,
            activation_bounds=leg.activation_bounds,
            safe_extra_spread=self.config.safe_extra_spread,
            triple_barrier_config=TripleBarrierConfig(
                take_profit=self.config.take_profit_pct,
                time_limit=self.config.time_limit_seconds,
                open_order_type=OrderType.LIMIT_MAKER,
                take_profit_order_type=OrderType.LIMIT_MAKER,
                stop_loss_order_type=OrderType.MARKET,
                time_limit_order_type=OrderType.MARKET,
            ),
            leverage=self.config.leverage,
            level_id=level_id,
            keep_position=True,
        )

    def _executor_matches(self, executor: Any, desired: GridExecutorConfig, now: float) -> bool:
        actual = getattr(executor, "config", None)
        if actual is None or self._executor_level_id(executor) != desired.level_id:
            return False
        if getattr(executor, "controller_id", None) != self.config.id:
            return False
        tolerance = self.config.price_tolerance_bps / Decimal("10000")
        for key in ("start_price", "end_price"):
            expected = _decimal(getattr(desired, key, None))
            observed = _decimal(getattr(actual, key, None))
            if expected <= 0 or abs(observed - expected) / expected > tolerance:
                return False
        expected_amount = _decimal(desired.total_amount_quote)
        observed_amount = _decimal(getattr(actual, "total_amount_quote", None))
        if (
            expected_amount <= 0
            or abs(observed_amount - expected_amount) / expected_amount
            > self.config.amount_tolerance_pct
        ):
            return False
        for key in (
            "side",
            "max_open_orders",
            "max_orders_per_batch",
            "order_frequency",
            "activation_bounds",
        ):
            if _enum_value(getattr(actual, key, None)) != _enum_value(getattr(desired, key, None)):
                return False
        timestamp = _finite_float(getattr(executor, "timestamp", None))
        return timestamp is None or now - timestamp <= self.config.max_executor_age_seconds

    def _record_would(self, kind: EvidenceKind, *, now: float, leg: Any | None, reason: str):
        self.evidence.add(
            kind,
            timestamp=now,
            source="controller_reconciliation",
            side=getattr(leg, "side", None),
            reason=reason,
            plan_version=self._grid_plan.plan_version,
        )

    def _record_native_executor_fills(self, now: float) -> None:
        """Record only executor-confirmed fills carrying a native order ID."""

        for executor in self._strategy_executors():
            custom_info = getattr(executor, "custom_info", {})
            filled_orders = (
                custom_info.get("filled_orders", []) if isinstance(custom_info, Mapping) else []
            )
            if not isinstance(filled_orders, (list, tuple)):
                continue
            for order in filled_orders:
                if not isinstance(order, Mapping):
                    continue
                order_id = (
                    order.get("order_id")
                    or order.get("exchange_order_id")
                    or order.get("client_order_id")
                )
                if not order_id:
                    continue
                evidence_key = f"{getattr(executor, 'id', '')}:{order_id}"
                if evidence_key in self._seen_fill_evidence:
                    continue
                self._seen_fill_evidence.add(evidence_key)
                self.evidence.add(
                    EvidenceKind.REAL_EXECUTOR_FILL,
                    timestamp=now,
                    source="native_grid_executor",
                    executor_id=getattr(executor, "id", None),
                    order_id=order_id,
                    filled_amount_quote=order.get("filled_amount_quote"),
                )

    def determine_executor_actions(self) -> list[ExecutorAction]:
        """Reconcile intended and executable grids, stopping before creation."""

        now = self._now()
        active = self._active_grid_executors()
        self._record_native_executor_fills(now)
        strategy_ids = self._strategy_executor_ids()
        managed_active = [
            executor
            for executor in active
            if self._is_managed_active_executor(executor, strategy_ids)
        ]
        unmanaged_active = [executor for executor in active if executor not in managed_active]
        desired_leg = self._desired_leg()
        intended_config = (
            self._native_config(desired_leg, self._grid_plan.decision_timestamp)
            if desired_leg
            else None
        )
        executable_allowed = bool(
            intended_config is not None
            and self._risk.ready
            and self.config.execution_enabled
            and self.config.mainnet_armed
            and not self.config.manual_kill_switch
            and (
                bool(managed_active)
                or self._account_cleanliness()["clean"] is True
            )
        )
        executable_desired_config = intended_config if executable_allowed else None
        stops: list[StopExecutorAction] = []
        actions: list[ExecutorAction] = []
        kept = 0
        creates = 0
        replacements = 0

        # Any unmanaged executor blocks this controller. It must never stop a
        # foreign strategy as a side effect of a state transition.
        for _executor in unmanaged_active:
            self._record_would(
                EvidenceKind.WOULD_STOP,
                now=now,
                leg=None,
                reason="unmanaged_active_executor_blocks_controller",
            )

        for executor in managed_active:
            expected_match = executable_desired_config is not None and self._executor_matches(
                executor, executable_desired_config, now
            )
            if expected_match:
                if kept == 0:
                    kept += 1
                    continue
                reason = "duplicate_strategy_executor"
            else:
                reason = (
                    "safety_gate_or_no_executable_grid"
                    if executable_desired_config is None
                    else "material_grid_drift_or_expired"
                )
            self._record_would(EvidenceKind.WOULD_STOP, now=now, leg=desired_leg, reason=reason)
            if self.config.execution_enabled:
                stops.append(
                    StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor.id,
                        # Safety stops cancel entries and retain any filled
                        # position. A separately reviewed exit path owns
                        # flattening; this action never silently market-closes.
                        keep_position=True,
                    )
                )

        # A stop must be observed before replacement. This applies to live
        # execution and to the dry-run evidence path alike.
        if stops:
            actions.extend(stops)
            # Never create a replacement in the same cycle as a safety stop.
            replacements = 0
        elif (
            executable_desired_config is not None
            and not managed_active
            and not unmanaged_active
        ):
            actions.append(
                CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=executable_desired_config,
                )
            )
            creates = 1
        elif intended_config is not None and not managed_active and not unmanaged_active:
            self._record_would(
                EvidenceKind.WOULD_CREATE,
                now=now,
                leg=desired_leg,
                reason="creation_gate_not_clear",
            )

        self._last_reconciliation = {
            "desired": int(intended_config is not None),
            "intended": int(intended_config is not None),
            "executable_desired": int(executable_desired_config is not None),
            "active": len(active),
            "pending": len(stops),
            "kept": kept,
            "stops": len(stops),
            "creates": creates,
            "replacements": replacements,
            "duplicates": max(0, len(managed_active) - 1),
            "unmanaged": len(unmanaged_active),
            "last_actions": [type(action).__name__ for action in actions],
        }
        return actions

    def _option_info(self) -> dict[str, Any]:
        option = self._options_snapshot
        bbo_midpoint = None if self._perp_snapshot is None else self._perp_snapshot.mid
        return {
            "data_available": option.option_data_available,
            "underlying": option.underlying,
            "environment": option.environment,
            "expiry": option.expiry,
            "expiry_timestamp": option.expiry_timestamp,
            "days_to_expiry": option.days_to_expiry,
            "atm_strike": option.atm_strike,
            "call_strike": option.call_strike,
            "put_strike": option.put_strike,
            "atm_distance_pct": option.atm_distance_pct,
            "reference_price": option.reference_price,
            "option_reference_price": option.reference_price,
            "bbo_midpoint": bbo_midpoint,
            "reference_matches_bbo": (
                option.reference_price is not None
                and bbo_midpoint is not None
                and abs(_decimal(option.reference_price) - bbo_midpoint)
                <= Decimal("0.000000000001")
            ),
            "call_instrument": option.call_instrument,
            "put_instrument": option.put_instrument,
            "call_iv": option.call_iv,
            "put_iv": option.put_iv,
            "atm_iv": option.atm_iv,
            "call_iv_source": option.call_iv_source,
            "put_iv_source": option.put_iv_source,
            "source_timestamp": option.source_timestamp,
            "received_timestamp": option.received_timestamp,
            "decision_timestamp": option.decision_timestamp,
            "age_seconds_at_controller_decision": (
                None if option.source_timestamp is None else self._now() - option.source_timestamp
            ),
            "source": option.source,
            "confidence": option.confidence,
            "chain_contract_count": option.chain_contract_count,
            "ticker_count": option.ticker_count,
            "valid_ticker_count": option.valid_ticker_count,
            "errors": option.errors,
        }

    def _grid_info(self) -> dict[str, Any]:
        desired = self._desired_leg()
        selected_half_width = {
            GridMode.AGGRESSIVE: self.config.aggressive_half_width_pct,
            GridMode.NORMAL: self.config.normal_half_width_pct,
            GridMode.DEFENSIVE: self.config.defensive_half_width_pct,
        }.get(self._grid_plan.mode)
        return {
            "plan_version": self._grid_plan.plan_version,
            "decision_timestamp": self._grid_plan.decision_timestamp,
            "mode": self._grid_plan.mode,
            "width_policy": "STATIC_REGIME",
            "selected_half_width_pct": selected_half_width,
            "expected_move_pct": None,
            "width_sigma_multiplier": None,
            "normal_half_width_pct": self.config.normal_half_width_pct,
            "defensive_half_width_pct": self.config.defensive_half_width_pct,
            "aggressive_half_width_pct": self.config.aggressive_half_width_pct,
            "aggressive_levels": self.config.aggressive_levels,
            "normal_levels": self.config.normal_levels,
            "defensive_levels": self.config.defensive_levels,
            "configured_aggressive_total_quote": self.config.aggressive_total_quote,
            "configured_normal_total_quote": self.config.total_amount_quote,
            "configured_defensive_total_quote": self.config.defensive_total_quote,
            "aggressive_total_quote": self._capital_snapshot.aggressive_budget_quote,
            "normal_total_quote": self._capital_snapshot.normal_budget_quote,
            "defensive_total_quote": self._capital_snapshot.defensive_budget_quote,
            "center_price": self._grid_plan.center_price,
            "valid": self._grid_plan.valid,
            "reason": self._grid_plan.reason,
            "errors": self._grid_plan.errors,
            "oneway_side": self.config.oneway_side,
            "projected_position_headroom_quote": self._projected_headroom_quote,
            "minimum_grid_order_quote": self._minimum_grid_order_quote,
            "size_block_reason": self._grid_size_block_reason,
            "maker_price_safe": self._maker_price_safe(desired),
            "selected_leg_total_quote": self._capital_snapshot.selected_leg_total_quote,
            "selected_native_minimum_quote": self._capital_snapshot.selected_native_minimum_quote,
            "selected_leg": desired,
            "oneway_suppression": (
                "configured side is not allowed by current inventory/mode"
                if self._grid_plan.valid and desired is None
                else "opposite side suppressed by Derive ONEWAY policy"
            ),
            "legs": self._grid_plan.legs,
        }

    def _executor_info(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for executor in self._strategy_executors():
            output.append(
                {
                    "id": getattr(executor, "id", None),
                    "controller_id": getattr(executor, "controller_id", None),
                    "type": getattr(executor, "type", None),
                    "level_id": self._executor_level_id(executor),
                    "side": getattr(getattr(executor, "config", None), "side", None),
                    "is_active": getattr(executor, "is_active", False),
                    "is_trading": getattr(executor, "is_trading", False),
                    "filled_amount_quote": getattr(executor, "filled_amount_quote", None),
                    "net_pnl_quote": getattr(executor, "net_pnl_quote", None),
                    "net_pnl_pct": getattr(executor, "net_pnl_pct", None),
                    "cum_fees_quote": getattr(executor, "cum_fees_quote", None),
                    "custom_info": getattr(executor, "custom_info", {}),
                }
            )
        return output

    def _account_cleanliness(self) -> dict[str, Any]:
        """Expose a conservative, read-only preflight view of the SOL account."""

        position_zero = abs(self._inventory.position_quote) == ZERO
        active_orders_count: int | None = None
        active_orders_source = "connector.in_flight_orders"
        order_errors: list[str] = []
        account_binding: dict[str, str] | None = None
        try:
            connector = self._connector()
            account_binding = account_binding_from_connector(connector)
            orders = getattr(connector, "in_flight_orders", None)
            if isinstance(orders, Mapping):
                active_orders_count = 0
                for order in orders.values():
                    trading_pair = getattr(order, "trading_pair", None)
                    if trading_pair is not None and str(trading_pair) != self.config.trading_pair:
                        continue
                    # Connector in-flight order maps are the exchange-backed
                    # active-order boundary. Count entries conservatively;
                    # do not assume an unknown order object is terminal.
                    active_orders_count += 1
            else:
                order_errors.append("active_orders_unavailable")
        except Exception as exc:
            order_errors.append(f"active_orders_read_error:{type(exc).__name__}")

        try:
            active = self._active_grid_executors()
        except Exception as exc:
            active = []
            order_errors.append(f"active_executors_read_error:{type(exc).__name__}")
        strategy_ids = self._strategy_executor_ids()
        managed_active = [
            executor
            for executor in active
            if self._is_managed_active_executor(executor, strategy_ids)
        ]
        unmanaged_active = [executor for executor in active if executor not in managed_active]
        managed_count = len(managed_active)
        unmanaged_count = len(unmanaged_active)
        clean = (
            position_zero
            and active_orders_count == 0
            and managed_count == 0
            and unmanaged_count == 0
        )
        blockers: list[str] = []
        if not position_zero:
            blockers.append("BLOCKED_BY_EXISTING_SOL_EXPOSURE")
        if active_orders_count is None:
            blockers.append("active_orders_unverified")
        elif active_orders_count > 0:
            blockers.append("BLOCKED_BY_EXISTING_SOL_ACTIVE_ORDERS")
        if managed_count > 0:
            blockers.append("managed_executors_active")
        if unmanaged_count > 0:
            blockers.append("unmanaged_executors_active")
        if account_binding is None:
            blockers.append("account_fingerprint_unavailable")
        blockers.extend(order_errors)
        return {
            "account_fingerprint": (
                account_binding.get("account_fingerprint") if account_binding else None
            ),
            "account_fingerprint_scheme": (
                account_binding.get("fingerprint_scheme") if account_binding else None
            ),
            "position_zero": position_zero,
            "active_orders_count": active_orders_count,
            "active_orders_zero": active_orders_count == 0,
            "active_orders_source": active_orders_source,
            "managed_executors_count": managed_count,
            "unmanaged_executors_count": unmanaged_count,
            "clean": clean,
            "blockers": tuple(dict.fromkeys(blockers)),
        }

    def get_custom_info(self) -> dict[str, Any]:
        """Stable Condor contract; diagnostics never authorize a mutation."""

        perp = self._perp_snapshot
        latest = self.evidence.latest
        position_quote = self._inventory.position_quote
        risk_blockers: list[str] = list(self._risk.reasons)
        operator_blockers: list[str] = []
        if not self.config.execution_enabled:
            operator_blockers.append("execution_disabled")
        if not self.config.mainnet_armed:
            operator_blockers.append("mainnet_not_armed")
        if self.config.manual_kill_switch:
            operator_blockers.append("manual_kill_switch_active")
        execution_blockers = [*risk_blockers, *operator_blockers]
        account_cleanliness = self._account_cleanliness()
        execution_blockers.extend(account_cleanliness["blockers"])
        desired_leg = self._desired_leg()
        can_create_executor = bool(
            desired_leg is not None
            and self._grid_plan.valid
            and self._risk.ready
            and self.config.execution_enabled
            and self.config.mainnet_armed
            and not self.config.manual_kill_switch
            and not self._last_reconciliation.get("active", 0)
            and account_cleanliness["clean"] is True
        )
        capital = self._capital_snapshot
        entry_safety = {
            "post_only_verified": self._connector_contract.post_only_verified,
            "maker_price_safe": self._maker_price_safe(desired_leg),
            "native_minimum_valid": bool(
                desired_leg is not None
                and (
                    native_minimum := self._native_minimum_for_leg(desired_leg)
                ) is not None
                and desired_leg.min_order_amount_quote >= native_minimum
            ),
            "semantics_ready": self._risk.entry_semantics_ready,
        }
        exit_safety = {
            "reduce_only_close_verified": self._connector_contract.close_contract_verified,
            "take_profit_reduce_only": self._connector_contract.close_contract_verified,
            "take_profit_post_only": self._connector_contract.post_only_verified,
            "time_limit_disabled": self.config.time_limit_seconds is None,
            "dearm_keep_position": True,
            "lifecycle_verified": self._connector_contract.lifecycle_verified,
            "semantics_ready": self._risk.exit_semantics_ready,
        }
        connector_proof = {
            "source": "immutable installed Derive connector runtime contract",
            "config_assertions_allowed": False,
            **self._connector_contract.as_dict(),
            "open_reduce_only": self._connector_contract.open_reduce_only,
            "close_reduce_only": self._connector_contract.close_contract_verified,
            "limit_maker_post_only": self._connector_contract.post_only_verified,
            "lifecycle": (
                "VERIFIED" if self._connector_contract.lifecycle_verified else "UNVERIFIED"
            ),
            "status": (
                "VERIFIED"
                if self._connector_contract.lifecycle_verified
                and self._connector_contract.contract_verified
                else "CONTRACT_VERIFIED"
                if self._connector_contract.contract_verified
                else "UNVERIFIED"
            ),
        }
        candidate_ready = self._risk.ready and account_cleanliness["clean"] is True
        live_readiness = {
            "status": "READY" if candidate_ready else "BLOCKED",
            "code_runtime_ready": self._risk.ready,
            "can_create_executor_now": can_create_executor,
            "armed": self.config.mainnet_armed,
            "execution_enabled": self.config.execution_enabled,
            "manual_kill_clear": not self.config.manual_kill_switch,
            "candidate_ready": candidate_ready,
            "blockers": tuple(
                dict.fromkeys([*execution_blockers, *account_cleanliness["blockers"]])
            ),
            "capital": {
                "ready": capital.capital_ready,
                "available_collateral_quote": capital.available_collateral_quote,
                "deployable_quote": capital.deployable_quote,
                "target_strategy_quote": capital.target_strategy_quote,
                "fee_buffer_quote": capital.fee_buffer_quote,
                "errors": capital.errors,
            },
            "account": {
                "clean": account_cleanliness["clean"],
                "account_fingerprint": account_cleanliness["account_fingerprint"],
                "account_fingerprint_scheme": account_cleanliness[
                    "account_fingerprint_scheme"
                ],
                "position_zero": account_cleanliness["position_zero"],
                "active_orders_count": account_cleanliness["active_orders_count"],
                "managed_executors_count": account_cleanliness["managed_executors_count"],
                "unmanaged_executors_count": account_cleanliness[
                    "unmanaged_executors_count"
                ],
                "blockers": account_cleanliness["blockers"],
            },
            "connector_lifecycle": {
                "open_reduce_only": self._connector_contract.open_reduce_only,
                "close_reduce_only": self._connector_contract.close_contract_verified,
                "limit_maker_post_only": self._connector_contract.post_only_verified,
                "lifecycle_test": (
                    "verified" if self._connector_contract.lifecycle_verified else "unverified"
                ),
                "runtime_instance_id": self._connector_contract.runtime_instance_id,
                "runtime_image_ref": self._connector_contract.runtime_image_ref,
                "account_fingerprint": self._connector_contract.runtime_account_fingerprint,
                "account_fingerprint_scheme": (
                    self._connector_contract.runtime_account_fingerprint_scheme
                ),
                "runtime_contract_id": self._connector_contract.runtime_contract_id,
                "runtime_manifest_verified": self._connector_contract.runtime_manifest_verified,
                "evidence_path": self._connector_contract.lifecycle_evidence_path,
                "evidence_present": self._connector_contract.lifecycle_evidence_present,
                "evidence_id": self._connector_contract.lifecycle_evidence_id,
                "evidence_sha256": self._connector_contract.lifecycle_evidence_sha256,
                "evidence_approved": self._connector_contract.lifecycle_evidence_approved,
                "generated_at": self._connector_contract.lifecycle_evidence_generated_at,
                "evidence_runtime_instance_id": (
                    self._connector_contract.lifecycle_evidence_runtime_instance_id
                ),
                "stage_a_status": self._connector_contract.lifecycle_stage_a_status,
                "stage_b_status": self._connector_contract.lifecycle_stage_b_status,
                "cleanup_status": self._connector_contract.lifecycle_cleanup_status,
                "runtime_identity_match": self._connector_contract.lifecycle_runtime_identity_match,
                "account_identity_match": self._connector_contract.lifecycle_account_identity_match,
                "blockers": self._connector_contract.lifecycle_evidence_blockers,
            },
        }
        return _wire(
            {
                "asset": "SOL",
                "updated_at": self._now(),
                "controller_timestamp_basis": "local Hummingbot decision clock",
                "runtime": {
                    "controller_status": _enum_value(getattr(self, "status", "UNKNOWN")),
                    "updated_at": self._now(),
                    "diagnostic_age_seconds": 0.0,
                },
                "execution": {
                    "connector_name": self.config.connector_name,
                    "trading_pair": self.config.trading_pair,
                    "exchange_instrument": self.config.exchange_instrument,
                    "environment": self.config.environment,
                    "position_mode": self.config.position_mode,
                    "leverage": self.config.leverage,
                    "oneway_side": self.config.oneway_side,
                    "mainnet_armed": self.config.mainnet_armed,
                    "execution_enabled": self.config.execution_enabled,
                    "manual_kill_switch": self.config.manual_kill_switch,
                    "capital_allocation_mode": self.config.capital_allocation_mode,
                },
                "perp_market_ready": bool(perp and perp.market_ready and not perp.errors),
                "connector_ready": self._risk.connector_ready,
                "perp": {
                    "connector_name": self.config.connector_name,
                    "trading_pair": self.config.trading_pair,
                    "exchange_instrument": self.config.exchange_instrument,
                    "bid": None if perp is None else perp.bid,
                    "ask": None if perp is None else perp.ask,
                    "mid": None if perp is None else perp.mid,
                    "spread": None if perp is None else perp.spread,
                    "source_timestamp": None if perp is None else perp.source_timestamp,
                    "received_timestamp": None if perp is None else perp.received_timestamp,
                    "decision_timestamp": None if perp is None else perp.decision_timestamp,
                    "age_seconds": None if perp is None else self._now() - perp.source_timestamp,
                    "timestamp_basis": "local Hummingbot-observed order-book update clock",
                    "order_book_marker": self._last_book_marker,
                    "spread_bps": None if perp is None else perp.spread_bps,
                    "price_increment": None if perp is None else perp.price_increment,
                    "market_data_provider_bid": (
                        None if perp is None else perp.market_data_provider_bid
                    ),
                    "market_data_provider_ask": (
                        None if perp is None else perp.market_data_provider_ask
                    ),
                    "bbo_crosscheck_delta": (None if perp is None else perp.bbo_crosscheck_delta),
                    "pricing_ready": False if perp is None else perp.pricing_ready,
                    "pricing_errors": () if perp is None else perp.pricing_errors,
                    "errors": () if perp is None else perp.errors,
                },
                "options_data_available": self._options_snapshot.option_data_available,
                "options": self._option_info(),
                "calibration_observation_ready": self._calibration_observation_ready,
                "calibration_observation": self._calibration_observation,
                "calibration_observation_errors": self._calibration_observation_errors,
                "iv_state": {
                    "state": self._state_decision.state,
                    "current_iv": self._state_decision.current_iv,
                    "baseline_iv": self._state_decision.baseline_iv,
                    "iv_ratio": self._state_decision.iv_ratio,
                    "history_size": self._state_decision.history_size,
                    "accepted": self._state_decision.accepted,
                    "decision_timestamp": self._state_decision.decision_timestamp,
                    "reasons": self._state_decision.reasons,
                    "history_source_timestamps": [row[0] for row in self.iv_state.observations],
                },
                "state_policy": {
                    "high_enter_ratio": self.config.high_enter_ratio,
                    "high_exit_ratio": self.config.high_exit_ratio,
                    "extreme_enter_ratio": self.config.extreme_enter_ratio,
                    "extreme_exit_ratio": self.config.extreme_exit_ratio,
                    "aggressive_enter_ratio": self.config.aggressive_enter_ratio,
                    "aggressive_exit_ratio": self.config.aggressive_exit_ratio,
                    "invariant": (
                        "0 < aggressive_enter_ratio < aggressive_exit_ratio <= 1.0 <= "
                        "high_exit_ratio < high_enter_ratio <= "
                        "extreme_exit_ratio < extreme_enter_ratio"
                    ),
                    "source": "reviewed controller configuration",
                },
                "market_state": self._state_decision.state,
                "grid_mode": self._mode_decision.mode,
                "mode": {
                    "mode": self._mode_decision.mode,
                    "state": self._mode_decision.state,
                    "perp_market_ready": self._mode_decision.perp_market_ready,
                    "options_data_available": self._mode_decision.options_data_available,
                    "buy_allowed": self._mode_decision.buy_allowed,
                    "sell_allowed": self._mode_decision.sell_allowed,
                    "decision_timestamp": self._mode_decision.decision_timestamp,
                    "reasons": self._mode_decision.reasons,
                },
                "inventory": {
                    "position_base": self._inventory.position_base,
                    "position_quote": position_quote,
                    "position_notional_quote": abs(position_quote),
                    "available_collateral_quote": self._inventory.available_collateral_quote,
                    "max_position_quote": self._inventory.max_position_quote,
                    "hard_position_quote": self._inventory.hard_position_quote,
                    "long_soft_limit": self._inventory.long_soft_limit,
                    "short_soft_limit": self._inventory.short_soft_limit,
                    "hard_limit_reached": self._inventory.hard_limit_reached,
                    "errors": self._inventory.errors,
                },
                "account_cleanliness": account_cleanliness,
                "capital": capital,
                "connector_proof": connector_proof,
                "lifecycle_evidence": {
                    "path": self._connector_contract.lifecycle_evidence_path,
                    "present": self._connector_contract.lifecycle_evidence_present,
                    "evidence_id": self._connector_contract.lifecycle_evidence_id,
                    "sha256": self._connector_contract.lifecycle_evidence_sha256,
                    "approved": self._connector_contract.lifecycle_evidence_approved,
                    "generated_at": self._connector_contract.lifecycle_evidence_generated_at,
                    "evidence_runtime_instance_id": (
                        self._connector_contract.lifecycle_evidence_runtime_instance_id
                    ),
                    "stage_a_status": self._connector_contract.lifecycle_stage_a_status,
                    "stage_b_status": self._connector_contract.lifecycle_stage_b_status,
                    "cleanup_status": self._connector_contract.lifecycle_cleanup_status,
                    "runtime_identity_match": (
                        self._connector_contract.lifecycle_runtime_identity_match
                    ),
                    "account_identity_match": (
                        self._connector_contract.lifecycle_account_identity_match
                    ),
                    "runtime_instance_id": self._connector_contract.runtime_instance_id,
                    "runtime_image_ref": self._connector_contract.runtime_image_ref,
                    "account_fingerprint": self._connector_contract.runtime_account_fingerprint,
                    "account_fingerprint_scheme": (
                        self._connector_contract.runtime_account_fingerprint_scheme
                    ),
                    "runtime_contract_id": self._connector_contract.runtime_contract_id,
                    "runtime_manifest_verified": self._connector_contract.runtime_manifest_verified,
                    "blockers": self._connector_contract.lifecycle_evidence_blockers,
                    "lifecycle_verified": self._connector_contract.lifecycle_verified,
                },
                "entry_safety": entry_safety,
                "exit_safety": exit_safety,
                "fee_economics": self._fee_economics,
                "grid": self._grid_info(),
                "risk_gates": {
                    "market_data_ready": self._risk.market_data_ready,
                    "options_ready": self._risk.options_ready,
                    "state_ready": self._risk.state_ready,
                    "pricing_ready": self._risk.pricing_ready,
                    "collateral_ready": self._risk.collateral_ready,
                    "inventory_ready": self._risk.inventory_ready,
                    "executor_ready": self._risk.executor_ready,
                    "manual_kill_clear": self._risk.manual_kill_clear,
                    "connector_ready": self._risk.connector_ready,
                    "grid_size_ready": self._risk.grid_size_ready,
                    "capital_ready": self._risk.capital_ready,
                    "fee_economics_ready": self._risk.fee_economics_ready,
                    "entry_semantics_ready": self._risk.entry_semantics_ready,
                    "exit_semantics_ready": self._risk.exit_semantics_ready,
                    "lifecycle_ready": self._risk.lifecycle_ready,
                    "planning_ready": self._risk.planning_ready,
                    "hard_block": self._risk.hard_block,
                    "ready": self._risk.ready,
                    "reasons": self._risk.reasons,
                },
                "intended_grid_available": bool(self._grid_plan.valid and desired_leg is not None),
                "intended_grid_mode": self._mode_decision.mode,
                "can_create_executor_now": can_create_executor,
                "mode_reasons": self._mode_decision.reasons,
                "risk_blockers": tuple(dict.fromkeys(risk_blockers)),
                "operator_blockers": tuple(dict.fromkeys(operator_blockers)),
                "execution_blockers": tuple(dict.fromkeys(execution_blockers)),
                "live_readiness": live_readiness,
                "reconciliation": self._last_reconciliation,
                "executors": self._executor_info(),
                "pnl": {
                    "real_executor_fill_count": self.evidence.real_fill_count,
                    "real_executor_fill_volume_quote": sum(
                        _decimal(getattr(executor, "filled_amount_quote", ZERO))
                        for executor in self._strategy_executors()
                    ),
                    "realized_pnl_quote": sum(
                        _decimal(getattr(executor, "net_pnl_quote", ZERO))
                        for executor in self._strategy_executors()
                    ),
                    "unrealized_pnl_quote": sum(
                        _decimal(
                            getattr(executor, "custom_info", {}).get("position_pnl_quote", ZERO)
                        )
                        for executor in self._strategy_executors()
                        if isinstance(getattr(executor, "custom_info", {}), Mapping)
                    ),
                    "fees_quote": sum(
                        _decimal(getattr(executor, "cum_fees_quote", ZERO))
                        for executor in self._strategy_executors()
                    ),
                    "evidence_policy": (
                        "only native executor/account evidence is REAL_EXECUTOR_FILL"
                    ),
                },
                "evidence": {
                    "counts": self.evidence.counts,
                    "latest": latest,
                    "recent": self.evidence.records[-20:],
                },
                "errors": tuple(dict.fromkeys(self._last_errors)),
            }
        )

    def to_format_status(self) -> list[str]:
        info = self.get_custom_info()
        return [
            "Derive SOL options adaptive grid",
            f"PERP_READY={info['perp_market_ready']} "
            f"OPTIONS_READY={info['options_data_available']}",
            f"STATE={info['market_state']} MODE={info['grid_mode']} "
            f"SIDE={info['execution']['oneway_side']}",
            f"IV={info['iv_state']['current_iv']} BASELINE={info['iv_state']['baseline_iv']} "
            f"RATIO={info['iv_state']['iv_ratio']}",
            f"POSITION_QUOTE={info['inventory']['position_quote']} "
            f"COLLATERAL={info['inventory']['available_collateral_quote']}",
            f"RISK_READY={info['risk_gates']['ready']} BLOCKS={info['risk_gates']['reasons']}",
            f"ARMED={info['execution']['mainnet_armed']} "
            f"ENABLED={info['execution']['execution_enabled']} "
            f"KILL={info['execution']['manual_kill_switch']}",
        ]


__all__ = ["DeriveOptionsAdaptiveGrid", "DeriveOptionsAdaptiveGridConfig"]
