"""Immutable, transport-friendly models shared by the controller and Condor.

The models intentionally keep perpetual readiness, option-data readiness, and
execution evidence separate.  A healthy perpetual feed is not evidence that
the option chain is usable, and a shadow or touch observation is not a fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class MarketState(StrEnum):
    INITIALIZING = "INITIALIZING"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class GridMode(StrEnum):
    AGGRESSIVE = "AGGRESSIVE"
    NORMAL = "NORMAL"
    DEFENSIVE = "DEFENSIVE"


class EvidenceKind(StrEnum):
    PUBLIC_MARKET_DATA = "PUBLIC_MARKET_DATA"
    SHADOW_PLAN = "SHADOW_PLAN"
    PROXY_TOUCH = "PROXY_TOUCH"
    WOULD_CREATE = "WOULD_CREATE"
    WOULD_STOP = "WOULD_STOP"
    REAL_EXECUTOR_FILL = "REAL_EXECUTOR_FILL"


@dataclass(frozen=True, slots=True)
class PerpSnapshot:
    """One causal SOL perpetual best-bid/best-ask observation."""

    connector_name: str
    trading_pair: str
    exchange_instrument: str
    bid: Decimal
    ask: Decimal
    source_timestamp: float
    received_timestamp: float
    decision_timestamp: float
    source: str = "hummingbot_market_data"
    errors: tuple[str, ...] = ()
    order_book_marker: tuple[Any, Any] | None = None
    price_increment: Decimal | None = None
    market_data_provider_bid: Decimal | None = None
    market_data_provider_ask: Decimal | None = None
    bbo_crosscheck_delta: Decimal | None = None
    pricing_ready: bool = True
    pricing_errors: tuple[str, ...] = ()

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal | None:
        if self.mid <= 0:
            return None
        return self.spread / self.mid * Decimal("10000")

    @property
    def age_seconds(self) -> float:
        return self.decision_timestamp - self.source_timestamp

    @property
    def market_ready(self) -> bool:
        return (
            self.connector_name == "derive_perpetual"
            and self.trading_pair == "SOL-USDC"
            and self.exchange_instrument == "SOL-PERP"
            and self.bid > 0
            and self.ask > self.bid
            and self.pricing_ready
            and self.source_timestamp <= self.received_timestamp
            and self.received_timestamp <= self.decision_timestamp
            and not self.errors
        )


@dataclass(frozen=True, slots=True)
class OptionsSnapshot:
    """A normalized ATM SOL IV observation from Derive public endpoints."""

    underlying: str
    reference_price: float | None
    expiry_timestamp: float | None
    expiry: str | None
    days_to_expiry: float | None
    atm_strike: float | None
    atm_distance_pct: float | None
    call_instrument: str | None
    put_instrument: str | None
    call_iv: float | None
    put_iv: float | None
    atm_iv: float | None
    call_iv_source: str | None
    put_iv_source: str | None
    source_timestamp: float | None
    received_timestamp: float | None
    decision_timestamp: float
    source: str
    environment: str
    data_available: bool
    confidence: float
    chain_contract_count: int = 0
    ticker_count: int = 0
    valid_ticker_count: int = 0
    errors: tuple[str, ...] = ()
    # Keep the selected pair's strikes explicit.  The controller exports these
    # fields for calibration provenance instead of asking a downstream
    # collector to infer them from the ATM strike.
    call_strike: float | None = None
    put_strike: float | None = None

    @property
    def option_data_age_seconds(self) -> float | None:
        if self.source_timestamp is None:
            return None
        return self.decision_timestamp - self.source_timestamp

    @property
    def option_data_available(self) -> bool:
        return self.data_available and self.atm_iv is not None


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    """Immutable dynamic-USDC allocation used by sizing and execution gates."""

    available_collateral_quote: Decimal | None = None
    reserve_quote: Decimal = Decimal("0")
    deployable_quote: Decimal = Decimal("0")
    target_strategy_quote: Decimal = Decimal("0")
    aggressive_budget_quote: Decimal = Decimal("0")
    normal_budget_quote: Decimal = Decimal("0")
    defensive_budget_quote: Decimal = Decimal("0")
    dynamic_soft_position_quote: Decimal = Decimal("0")
    dynamic_hard_position_quote: Decimal = Decimal("0")
    fee_buffer_quote: Decimal = Decimal("0")
    selected_leg_total_quote: Decimal = Decimal("0")
    selected_native_minimum_quote: Decimal = Decimal("0")
    selected_leg_fundable: bool = False
    capital_ready: bool = False
    source_timestamp: float | None = None
    decision_timestamp: float | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """Current inventory and collateral facts used by the sizing gate."""

    position_base: Decimal = Decimal("0")
    position_quote: Decimal = Decimal("0")
    available_collateral_quote: Decimal | None = None
    max_position_quote: Decimal = Decimal("0")
    hard_position_quote: Decimal = Decimal("0")
    source_timestamp: float | None = None
    received_timestamp: float | None = None
    decision_timestamp: float | None = None
    source: str = "hummingbot_account"
    errors: tuple[str, ...] = ()

    @property
    def hard_limit_reached(self) -> bool:
        return self.hard_position_quote > 0 and abs(self.position_quote) >= self.hard_position_quote

    @property
    def long_soft_limit(self) -> bool:
        return self.max_position_quote > 0 and self.position_quote >= self.max_position_quote

    @property
    def short_soft_limit(self) -> bool:
        return self.max_position_quote > 0 and self.position_quote <= -self.max_position_quote


@dataclass(frozen=True, slots=True)
class RiskGateState:
    """Explicit risk gates; ``hard_block`` is never inferred from missing data."""

    market_data_ready: bool = False
    collateral_ready: bool = False
    inventory_ready: bool = False
    executor_ready: bool = True
    manual_kill_clear: bool = False
    hard_block: bool = True
    reasons: tuple[str, ...] = ()
    # Defaults preserve the pure-model constructor contract; the runtime
    # controller sets both gates explicitly on every market-data refresh.
    connector_ready: bool = True
    grid_size_ready: bool = True
    options_ready: bool = True
    state_ready: bool = True
    pricing_ready: bool = True
    # Pure-model defaults remain permissive for callers that construct a
    # fully-populated gate positionally. The runtime controller sets these
    # fields explicitly from live evidence on every refresh.
    capital_ready: bool = True
    fee_economics_ready: bool = True
    entry_semantics_ready: bool = True
    exit_semantics_ready: bool = True
    lifecycle_ready: bool = True

    @property
    def planning_ready(self) -> bool:
        return (
            self.market_data_ready
            and self.options_ready
            and self.state_ready
            and self.pricing_ready
            and self.collateral_ready
            and self.capital_ready
            and self.inventory_ready
            and self.executor_ready
            and self.connector_ready
            and self.grid_size_ready
            and not self.hard_block
        )

    @property
    def ready(self) -> bool:
        """Execution-ready, including the operator's manual kill switch."""

        return (
            self.planning_ready
            and self.manual_kill_clear
            and self.fee_economics_ready
            and self.entry_semantics_ready
            and self.exit_semantics_ready
            and self.lifecycle_ready
        )


@dataclass(frozen=True, slots=True)
class StateDecision:
    state: MarketState
    current_iv: float | None
    baseline_iv: float | None
    iv_ratio: float | None
    history_size: int
    accepted: bool
    decision_timestamp: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModeDecision:
    mode: GridMode
    state: MarketState
    perp_market_ready: bool
    options_data_available: bool
    buy_allowed: bool
    sell_allowed: bool
    decision_timestamp: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GridLegPlan:
    """The pure-to-Hummingbot mapping for one side of a GridExecutor."""

    side: str
    start_price: Decimal
    end_price: Decimal
    limit_price: Decimal
    total_amount_quote: Decimal
    min_order_amount_quote: Decimal
    max_open_orders: int
    max_orders_per_batch: int
    min_spread_between_orders: Decimal
    order_frequency: int
    activation_bounds: Decimal | None
    expected_level_prices: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class GridPlan:
    plan_version: str
    mode: GridMode
    center_price: Decimal
    decision_timestamp: float
    legs: tuple[GridLegPlan, ...]
    valid: bool
    reason: str
    errors: tuple[str, ...] = ()

    @property
    def buy_leg(self) -> GridLegPlan | None:
        return next((leg for leg in self.legs if leg.side == "BUY"), None)

    @property
    def sell_leg(self) -> GridLegPlan | None:
        return next((leg for leg in self.legs if leg.side == "SELL"), None)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    kind: EvidenceKind
    timestamp: float
    source: str
    details: Mapping[str, Any] = field(default_factory=dict)
