# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""Pure safety logic for the separately authorized Derive lifecycle canary.

This module has no exchange client and no order transport.  It contains the
rules that a Hummingbot-native runner must satisfy before it may use normal
Hummingbot order/executor APIs.  Keeping these checks pure makes dry-run tests
repeatable and makes accidental REST or strategy-local execution impossible.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from .lifecycle_evidence import (
    EXPECTED_IDENTITY,
    EXPECTED_STATIC_CONTRACT,
    LifecycleEvidence,
    build_lifecycle_evidence,
)

ZERO = Decimal("0")


class CanaryMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    STAGE_A = "STAGE_A"
    STAGE_B = "STAGE_B"


class CanaryStage(StrEnum):
    STAGE_A = "STAGE_A"
    STAGE_B = "STAGE_B"


@dataclass(frozen=True)
class AuthorizationDecision:
    mode: CanaryMode
    stage: CanaryStage | None
    authorized: bool
    mutation_allowed: bool
    mutation_attempted: bool = False
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "stage": self.stage.value if self.stage else None,
            "authorized": self.authorized,
            "mutation_allowed": self.mutation_allowed,
            "mutation_attempted": self.mutation_attempted,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class DryRunPlan:
    stage: CanaryStage
    side: str | None
    amount_base: Decimal | None
    price: Decimal | None
    notional_quote: Decimal | None
    position_action: str
    order_type: str
    reduce_only: bool
    time_in_force: str
    passive: bool
    blockers: tuple[str, ...] = ()
    maker_gap_ticks: int = 0
    maker_guard_ticks: int = 0
    maker_guard_bps: Decimal = ZERO

    @property
    def valid(self) -> bool:
        return not self.blockers and self.amount_base is not None and self.price is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "side": self.side,
            "amount_base": _wire(self.amount_base),
            "price": _wire(self.price),
            "notional_quote": _wire(self.notional_quote),
            "position_action": self.position_action,
            "order_type": self.order_type,
            "reduce_only": self.reduce_only,
            "time_in_force": self.time_in_force,
            "passive": self.passive,
            "valid": self.valid,
            "blockers": list(self.blockers),
            "maker_gap_ticks": self.maker_gap_ticks,
            "maker_guard_ticks": self.maker_guard_ticks,
            "maker_guard_bps": _wire(self.maker_guard_bps),
        }


@dataclass(frozen=True)
class DryRunResult:
    mode: CanaryMode
    preflight_blockers: tuple[str, ...]
    authorization: AuthorizationDecision
    stage_a_plan: DryRunPlan
    stage_b_plan: DryRunPlan
    mutation_attempted: bool = False

    @property
    def ready_for_explicit_authorization(self) -> bool:
        return not self.preflight_blockers and not self.authorization.mutation_allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "preflight_blockers": list(self.preflight_blockers),
            "authorization": self.authorization.as_dict(),
            "stage_a_plan": self.stage_a_plan.as_dict(),
            "stage_b_plan": self.stage_b_plan.as_dict(),
            "mutation_attempted": self.mutation_attempted,
            "ready_for_explicit_authorization": self.ready_for_explicit_authorization,
        }


class LifecycleAdapter(Protocol):
    """The only interface the finite-state canary may use for mutations."""

    def submit_open(self, trading_pair: str, plan: DryRunPlan) -> str: ...

    def submit_close(self, trading_pair: str, plan: DryRunPlan) -> str: ...

    def cancel(self, trading_pair: str, client_order_id: str) -> Any: ...

    def wait_for_order(
        self, trading_pair: str, client_order_id: str, timeout_seconds: float
    ) -> Mapping[str, Any]: ...

    def read_account(self, trading_pair: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LifecycleExecutionResult:
    """Finite-state result with counters and raw observations for review."""

    stage: CanaryStage
    status: str
    blockers: tuple[str, ...]
    observations: Mapping[str, Any]
    mutation_attempted: bool = False
    orders_submitted: int = 0
    orders_cancelled: int = 0
    fills: int = 0
    position_changes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status,
            "blockers": list(self.blockers),
            "observations": dict(self.observations),
            "mutation_attempted": self.mutation_attempted,
            "orders_submitted": self.orders_submitted,
            "orders_cancelled": self.orders_cancelled,
            "fills": self.fills,
            "position_changes": self.position_changes,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _wire(value: Any) -> str | None:
    return None if value is None else format(value, "f")


def _count(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None


def _quantize_up(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= ZERO:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    return units * increment


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= ZERO:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return units * increment


def validate_runtime_identity(
    observed: Mapping[str, Any], expected: Mapping[str, Any]
) -> tuple[str, ...]:
    """Compare runtime identity fields without accepting a partial match."""

    blockers: list[str] = []
    for key, expected_value in expected.items():
        if observed.get(key) != expected_value:
            blockers.append(f"runtime_identity_mismatch:{key}")
    for key in (
        "base_image_digest",
        "connector_source_sha256",
        "fee_source_sha256",
        "runtime_instance_id",
        "runtime_image_ref",
    ):
        if not observed.get(key):
            blockers.append(f"runtime_identity_missing:{key}")
    if not observed.get("runtime_contract_id"):
        blockers.append("runtime_identity_missing:runtime_contract_id")
    return tuple(dict.fromkeys(blockers))


def validate_preflight_runtime_binding(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Require every read-only preflight input to come from one runtime.

    A runtime contract hash alone identifies code, not the live connector
    instance that supplied the account, BBO, and trading-rule reads.  Each
    section therefore carries the same container identity and contract ID.
    Missing or mixed provenance is a hard blocker.
    """

    runtime = _mapping(snapshot.get("runtime"))
    expected = _mapping(snapshot.get("expected_runtime"))
    required = ("runtime_instance_id", "runtime_contract_id", "runtime_image_ref")
    blockers: list[str] = []
    if any(not runtime.get(key) for key in required):
        blockers.append("BLOCKED_BY_PREFLIGHT_RUNTIME_INSTANCE_BINDING")
    if any(runtime.get(key) != expected.get(key) for key in required):
        blockers.append("BLOCKED_BY_PREFLIGHT_RUNTIME_INSTANCE_BINDING")

    for section_name in ("account", "bbo", "rules", "static_contract"):
        section = _mapping(snapshot.get(section_name))
        section_mismatch = False
        for key in required:
            if not section.get(key) or section.get(key) != runtime.get(key):
                section_mismatch = True
                blockers.append(
                    f"preflight_runtime_binding_mismatch:{section_name}:{key}"
                )
                break
        if section_mismatch:
            blockers.append("BLOCKED_BY_PREFLIGHT_RUNTIME_INSTANCE_BINDING")
    return tuple(dict.fromkeys(blockers))


def validate_market_identity(identity: Mapping[str, Any]) -> tuple[str, ...]:
    blockers = [
        f"market_identity_mismatch:{key}"
        for key, expected in EXPECTED_IDENTITY.items()
        if identity.get(key) != expected
    ]
    return tuple(blockers)


def validate_static_contract(observed: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    for key, expected in EXPECTED_STATIC_CONTRACT.items():
        actual = observed.get(key)
        if key.endswith("_fee_decimal"):
            if _decimal(actual) != Decimal(str(expected)):
                blockers.append(f"static_contract_mismatch:{key}")
        elif actual != expected:
            blockers.append(f"static_contract_mismatch:{key}")
    return tuple(blockers)


def validate_account_clean(account: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    position_base = _decimal(account.get("position_base", account.get("position")))
    position_quote = _decimal(account.get("position_quote", account.get("position_notional_quote")))
    if position_base is None and position_quote is None:
        blockers.append("account_position_unavailable")
    elif abs(position_base or position_quote or ZERO) != ZERO:
        blockers.append("account_position_nonzero")
    for label, keys in (
        ("active_orders", ("active_orders", "active_orders_count")),
        ("managed_executors", ("managed_executors", "managed_executors_count")),
        ("unmanaged_executors", ("unmanaged_executors", "unmanaged_executors_count")),
    ):
        count = _count(account, *keys)
        if count is None:
            blockers.append(f"account_{label}_unavailable")
        elif count != 0:
            blockers.append(f"account_{label}_nonzero")
    if not account.get("account_fingerprint"):
        blockers.append("account_fingerprint_missing")
    return tuple(dict.fromkeys(blockers))


def validate_market_freshness(
    bbo: Mapping[str, Any], *, now: float | None = None, max_age_seconds: float = 10.0
) -> tuple[str, ...]:
    now = time.time() if now is None else now
    bid = _decimal(bbo.get("best_bid", bbo.get("bid")))
    ask = _decimal(bbo.get("best_ask", bbo.get("ask")))
    marker = bbo.get("order_book_marker")
    if marker is None:
        marker = (bbo.get("snapshot_uid"), bbo.get("last_update_id"))
    marker_missing = not (
        isinstance(marker, (tuple, list))
        and len(marker) == 2
        and (marker[0] is not None or marker[1] is not None)
    )
    observed_at = bbo.get("observed_at", bbo.get("timestamp"))
    blockers: list[str] = []
    if marker_missing:
        blockers.append("bbo_update_marker_unavailable")
    if bid is None or ask is None:
        blockers.append("bbo_unavailable")
    elif bid <= ZERO or ask <= bid:
        blockers.append("bbo_invalid")
    try:
        age = now - float(observed_at)
    except (TypeError, ValueError):
        blockers.append("bbo_timestamp_unavailable")
    else:
        if age < 0:
            blockers.append("bbo_timestamp_in_future")
        elif age > max_age_seconds:
            blockers.append("bbo_stale")
    return tuple(dict.fromkeys(blockers))


def minimum_safe_amount(
    rules: Mapping[str, Any], price: Decimal, *, quote_buffer: Decimal = ZERO
) -> Decimal | None:
    """Return the smallest amount satisfying native amount and notional rules."""

    if price <= ZERO:
        return None
    min_base = _decimal(rules.get("min_base_amount", rules.get("min_amount"))) or ZERO
    increment = _decimal(rules.get("amount_increment", rules.get("amount_step"))) or ZERO
    min_notional = _decimal(rules.get("min_notional", rules.get("min_order_notional"))) or ZERO
    required = max(min_base, (min_notional + quote_buffer) / price)
    return _quantize_up(required, increment)


def passive_price(
    side: str,
    bid: Decimal,
    ask: Decimal,
    price_increment: Decimal,
    *,
    ticks_behind_touch: int = 0,
    maker_guard_ticks: int | None = None,
    maker_guard_bps: Decimal = ZERO,
) -> Decimal | None:
    """Choose a conservative passive price on the current BBO.

    The guard is the larger of a native tick distance and a basis-point
    distance from the BBO midpoint.  The Stage A canary uses this additional
    gap only for its one-order probe; the adaptive grid has its own pricing
    policy.
    """

    normalized = str(side).upper()
    if bid <= ZERO or ask <= bid:
        return None
    ticks = max(int(ticks_behind_touch if maker_guard_ticks is None else maker_guard_ticks), 0)
    gap = maker_guard_distance(
        bid,
        ask,
        price_increment,
        guard_ticks=ticks,
        guard_bps=maker_guard_bps,
    )
    if normalized == "BUY":
        price = bid - gap
        return _quantize_down(price, price_increment) if price > ZERO else None
    if normalized == "SELL":
        price = ask + gap
        return _quantize_up(price, price_increment)
    return None


def maker_guard_distance(
    bid: Decimal,
    ask: Decimal,
    price_increment: Decimal,
    *,
    guard_ticks: int = 0,
    guard_bps: Decimal = ZERO,
) -> Decimal:
    """Return ``max(tick * ticks, reference_price * bps / 10000)``."""

    ticks = max(int(guard_ticks), 0)
    bps = max(_decimal(guard_bps) or ZERO, ZERO)
    reference_price = (bid + ask) / Decimal("2")
    tick_gap = max(price_increment, ZERO) * ticks
    bps_gap = max(reference_price, ZERO) * bps / Decimal("10000")
    return max(tick_gap, bps_gap)


def validate_maker_price(
    side: str,
    price: Decimal | None,
    bid: Decimal,
    ask: Decimal,
    price_increment: Decimal,
    *,
    ticks_behind_touch: int = 0,
    maker_guard_ticks: int | None = None,
    maker_guard_bps: Decimal = ZERO,
) -> tuple[str, ...]:
    """Verify that a proposed post-only price has an explicit safety gap.

    A BBO snapshot can move between the local quote calculation and the
    exchange request.  The check is therefore deliberately separate from
    :func:`passive_price`: callers must validate the final price against the
    freshest BBO they are about to use for submission.
    """

    if price is None:
        return ("maker_price_unavailable",)
    if bid <= ZERO or ask <= bid:
        return ("maker_bbo_invalid",)
    if price_increment <= ZERO:
        return ("maker_price_increment_invalid",)
    ticks = max(int(ticks_behind_touch if maker_guard_ticks is None else maker_guard_ticks), 0)
    gap = maker_guard_distance(
        bid,
        ask,
        price_increment,
        guard_ticks=ticks,
        guard_bps=maker_guard_bps,
    )
    normalized = str(side).upper()
    blockers: list[str] = []
    if normalized == "BUY":
        if price >= ask:
            blockers.append("maker_buy_would_cross_ask")
        if gap > ZERO and price > bid - gap:
            blockers.append("maker_buy_gap_too_small")
    elif normalized == "SELL":
        if price <= bid:
            blockers.append("maker_sell_would_cross_bid")
        if gap > ZERO and price < ask + gap:
            blockers.append("maker_sell_gap_too_small")
    else:
        blockers.append("maker_side_invalid")
    return tuple(dict.fromkeys(blockers))


def evaluate_authorization(
    mode: CanaryMode | str,
    stage: CanaryStage | str | None = None,
    *,
    cli_authorized: bool = False,
    runtime_stage_authorization: str | None = None,
    one_run_token: str | None = None,
    expected_one_run_token: str | None = None,
    runtime_valid: bool = False,
    stage_a_passed: bool = False,
) -> AuthorizationDecision:
    """Apply the two independent authorization gates.

    The token itself is never returned or logged.  A dry run is always
    read-only, even if a caller mistakenly passes authorization values.
    """

    selected_mode = mode if isinstance(mode, CanaryMode) else CanaryMode(str(mode).upper())
    selected_stage = None
    if stage is not None:
        selected_stage = (
            stage if isinstance(stage, CanaryStage) else CanaryStage(str(stage).upper())
        )
    if selected_mode is CanaryMode.DRY_RUN:
        return AuthorizationDecision(
            mode=selected_mode,
            stage=selected_stage,
            authorized=False,
            mutation_allowed=False,
            blockers=("dry_run_mode",),
        )

    blockers: list[str] = []
    if selected_stage is None or selected_stage.value != selected_mode.value:
        blockers.append("stage_mode_mismatch")
    if not cli_authorized:
        blockers.append("explicit_stage_authorization_missing")
    if str(runtime_stage_authorization or "").upper() != (
        selected_stage.value if selected_stage else ""
    ):
        blockers.append("runtime_stage_authorization_missing_or_wrong")
    if not one_run_token or not expected_one_run_token or one_run_token != expected_one_run_token:
        blockers.append("one_run_token_missing_or_wrong")
    if not runtime_valid:
        blockers.append("runtime_identity_not_verified")
    if selected_stage is CanaryStage.STAGE_B and not stage_a_passed:
        blockers.append("stage_a_reviewed_pass_required")
    authorized = not blockers
    return AuthorizationDecision(
        mode=selected_mode,
        stage=selected_stage,
        authorized=authorized,
        mutation_allowed=authorized,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def build_stage_a_plan(
    *,
    side: str,
    bbo: Mapping[str, Any],
    rules: Mapping[str, Any],
    quote_buffer: Decimal = ZERO,
    ticks_behind_touch: int = 5,
    maker_guard_ticks: int | None = None,
    maker_guard_bps: Decimal = Decimal("15"),
) -> DryRunPlan:
    bid = _decimal(bbo.get("best_bid", bbo.get("bid")))
    ask = _decimal(bbo.get("best_ask", bbo.get("ask")))
    tick = _decimal(rules.get("price_increment", rules.get("price_step"))) or ZERO
    if bid is None or ask is None:
        return DryRunPlan(
            CanaryStage.STAGE_A,
            side,
            None,
            None,
            None,
            "OPEN",
            "LIMIT_MAKER",
            False,
            "post_only",
            True,
            ("bbo_unavailable",),
        )
    # A Stage A canary should prove resting/cancellation behaviour, not seek a
    # fill. Keep the limit behind the touch by the larger native-tick or bps
    # guard. This guard is intentionally not used by the adaptive grid.
    maker_guard_ticks = max(
        int(ticks_behind_touch if maker_guard_ticks is None else maker_guard_ticks), 0
    )
    normalized_guard_bps = _decimal(maker_guard_bps) or ZERO
    price = passive_price(
        side,
        bid,
        ask,
        tick,
        maker_guard_ticks=maker_guard_ticks,
        maker_guard_bps=normalized_guard_bps,
    )
    amount = minimum_safe_amount(rules, price, quote_buffer=quote_buffer) if price else None
    blockers = list(
        validate_maker_price(
            side,
            price,
            bid,
            ask,
            tick,
            maker_guard_ticks=maker_guard_ticks,
            maker_guard_bps=normalized_guard_bps,
        )
    )
    if price is None:
        blockers.append("passive_price_unavailable")
    if amount is None:
        blockers.append("minimum_safe_amount_unavailable")
    notional = amount * price if amount is not None and price is not None else None
    return DryRunPlan(
        stage=CanaryStage.STAGE_A,
        side=str(side).upper(),
        amount_base=amount,
        price=price,
        notional_quote=notional,
        position_action="OPEN",
        order_type="LIMIT_MAKER",
        reduce_only=False,
        time_in_force="post_only",
        passive=True,
        blockers=tuple(blockers),
        maker_gap_ticks=maker_guard_ticks,
        maker_guard_ticks=maker_guard_ticks,
        maker_guard_bps=normalized_guard_bps,
    )


def build_stage_b_plan(
    *, side: str, bbo: Mapping[str, Any], rules: Mapping[str, Any], quote_buffer: Decimal = ZERO
) -> DryRunPlan:
    close_side = "SELL" if str(side).upper() == "BUY" else "BUY"
    bid = _decimal(bbo.get("best_bid", bbo.get("bid")))
    ask = _decimal(bbo.get("best_ask", bbo.get("ask")))
    tick = _decimal(rules.get("price_increment", rules.get("price_step"))) or ZERO
    price = (
        passive_price(close_side, bid, ask, tick)
        if bid is not None and ask is not None
        else None
    )
    amount = minimum_safe_amount(rules, price, quote_buffer=quote_buffer) if price else None
    blockers: list[str] = []
    if price is None:
        blockers.append("passive_price_unavailable")
    if amount is None:
        blockers.append("minimum_safe_amount_unavailable")
    notional = amount * price if amount is not None and price is not None else None
    return DryRunPlan(
        stage=CanaryStage.STAGE_B,
        side=close_side,
        amount_base=amount,
        price=price,
        notional_quote=notional,
        position_action="CLOSE",
        order_type="LIMIT_MAKER",
        reduce_only=True,
        time_in_force="post_only",
        passive=True,
        blockers=tuple(blockers),
    )


def validate_stage_a_result(result: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    if str(result.get("status", "")).upper() != "PASS":
        blockers.append("stage_a_status_not_pass")
    if result.get("position_action") != "OPEN":
        blockers.append("stage_a_position_action_not_open")
    if result.get("reduce_only") is not False:
        blockers.append("stage_a_reduce_only_not_false")
    if result.get("order_type") != "LIMIT_MAKER":
        blockers.append("stage_a_order_type_not_limit_maker")
    if result.get("time_in_force") != "post_only":
        blockers.append("stage_a_time_in_force_not_post_only")
    if result.get("post_only_requested") is not True:
        blockers.append("stage_a_post_only_not_requested")
    if result.get("resting_verified") is not True:
        blockers.append("stage_a_resting_not_verified")
    if not result.get("native_order_id"):
        blockers.append("stage_a_native_order_id_missing")
    if result.get("fill_count", 0) != 0:
        blockers.append("stage_a_unexpected_fill")
    if result.get("cancel_acknowledged") is not True:
        blockers.append("stage_a_cancel_not_acknowledged")
    if _count(result, "active_orders", "active_orders_count") != 0:
        blockers.append("stage_a_active_order_remaining")
    if (_decimal(result.get("position_after")) or ZERO) != ZERO:
        blockers.append("stage_a_position_not_flat")
    return tuple(dict.fromkeys(blockers))


def validate_position_transition(
    position_before: Any, position_after: Any, *, action: str, reduce_only: bool = False
) -> tuple[str, ...]:
    before = _decimal(position_before)
    after = _decimal(position_after)
    if before is None or after is None:
        return ("position_transition_unavailable",)
    blockers: list[str] = []
    normalized_action = str(action).upper()
    if reduce_only and before == ZERO and after != ZERO:
        blockers.append("ABORT_REDUCE_ONLY_OPENED_EXPOSURE")
    if normalized_action == "CLOSE":
        if abs(after) > abs(before):
            blockers.append("ABORT_POSITION_INCREASED_DURING_CLOSE")
        if before != ZERO and after != ZERO and before * after < ZERO:
            blockers.append("ABORT_POSITION_SIGN_FLIP")
    return tuple(dict.fromkeys(blockers))


def validate_stage_b_result(result: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    if str(result.get("status", "")).upper() != "PASS":
        blockers.append("stage_b_status_not_pass")
    if result.get("position_action") != "CLOSE":
        blockers.append("stage_b_position_action_not_close")
    if result.get("reduce_only") is not True:
        blockers.append("stage_b_reduce_only_not_true")
    order_type = result.get("order_type")
    time_in_force = result.get("time_in_force")
    # Stage B is the one canary that must establish and then close a real
    # minimum position. A bounded ordinary LIMIT order is therefore valid for
    # the close: it retains a maximum execution price while giving the close a
    # practical fill path. LIMIT_MAKER remains valid when it does fill, and
    # still carries its explicit post-only invariant.
    if order_type not in {"LIMIT", "LIMIT_MAKER"}:
        blockers.append("stage_b_order_type_not_bounded_limit")
    elif order_type == "LIMIT" and time_in_force != "gtc":
        blockers.append("stage_b_limit_time_in_force_not_gtc")
    elif order_type == "LIMIT_MAKER" and time_in_force != "post_only":
        blockers.append("stage_b_limit_maker_time_in_force_not_post_only")
    if result.get("close_reduce_only_requested") is not True:
        blockers.append("stage_b_reduce_only_not_requested")
    if not result.get("close_native_order_id", result.get("native_order_id")):
        blockers.append("stage_b_native_order_id_missing")
    fill_count = _count(result, "fill_count", "close_fill_count")
    if fill_count is None or fill_count < 1:
        blockers.append("stage_b_close_fill_not_reconciled")
    oversized_tested = result.get("oversized_close_tested") is True
    if oversized_tested and (
        result.get("oversized_close_rejected_or_clamped") is not True
        or result.get("oversized_close_position_increased") is True
        or result.get("oversized_close_position_flipped") is True
    ):
        blockers.append("stage_b_oversized_close_safety_not_proven")
    blockers.extend(
        validate_position_transition(
            result.get("close_position_before", result.get("position_before")),
            result.get("close_position_after", result.get("position_after")),
            action="CLOSE",
            reduce_only=True,
        )
    )
    if (_decimal(result.get("position_after")) or ZERO) != ZERO:
        blockers.append("stage_b_position_not_flat")
    return tuple(dict.fromkeys(blockers))


def validate_cleanup(cleanup: Mapping[str, Any]) -> tuple[str, ...]:
    blockers: list[str] = []
    if cleanup.get("position_zero") is not True:
        blockers.append("cleanup_position_nonzero")
    for label in ("active_orders", "managed_executors", "unmanaged_executors"):
        count = _count(cleanup, label)
        if count != 0:
            blockers.append(f"cleanup_{label}_nonzero")
    if blockers:
        blockers.insert(0, "MAINNET_LIFECYCLE_CLEANUP_FAILED")
    return tuple(dict.fromkeys(blockers))


def _execution_preflight_blockers(
    preflight: Mapping[str, Any], *, side: str
) -> tuple[str, ...]:
    supplied = preflight.get("preflight_blockers")
    if supplied is not None:
        return tuple(dict.fromkeys(str(item) for item in supplied or ()))
    return build_dry_run_result(preflight, side=side).preflight_blockers


def _position_from_account(account: Mapping[str, Any]) -> Decimal | None:
    return _decimal(account.get("position_base", account.get("position")))


def _count_value(account: Mapping[str, Any], label: str) -> int | None:
    return _count(account, label, f"{label}_count")


def _cancel_once(
    adapter: LifecycleAdapter, trading_pair: str, client_order_id: str
) -> tuple[bool, str | None]:
    try:
        result = adapter.cancel(trading_pair, client_order_id)
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        return False, f"cancel_error:{type(exc).__name__}"
    if isinstance(result, Mapping):
        acknowledged = result.get("acknowledged", result.get("cancel_acknowledged"))
        return bool(acknowledged), None if acknowledged else "cancel_not_acknowledged"
    return result is not False, None if result is not False else "cancel_not_acknowledged"


def run_stage_a(
    adapter: LifecycleAdapter,
    *,
    trading_pair: str,
    plan: DryRunPlan,
    preflight: Mapping[str, Any],
    authorization: AuthorizationDecision,
    timeout_seconds: float = 30.0,
) -> LifecycleExecutionResult:
    """Run the bounded post-only/cancel state machine through Hummingbot APIs."""

    blockers = list(_execution_preflight_blockers(preflight, side=plan.side or "BUY"))
    blockers.extend(plan.blockers)
    if not authorization.mutation_allowed:
        blockers.extend(authorization.blockers or ("authorization_denied",))
        return LifecycleExecutionResult(
            CanaryStage.STAGE_A,
            "BLOCKED",
            tuple(dict.fromkeys(blockers)),
            {"authorization": authorization.as_dict()},
        )
    if blockers:
        return LifecycleExecutionResult(
            CanaryStage.STAGE_A,
            "BLOCKED",
            tuple(dict.fromkeys(blockers)),
            {"authorization": authorization.as_dict()},
        )

    observations: dict[str, Any] = {"authorization": authorization.as_dict()}
    orders_submitted = 0
    orders_cancelled = 0
    try:
        native_order_id = adapter.submit_open(trading_pair, plan)
        orders_submitted = 1
        observation = dict(
            adapter.wait_for_order(trading_pair, native_order_id, timeout_seconds)
        )
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        return LifecycleExecutionResult(
            CanaryStage.STAGE_A,
            "FAIL",
            (f"stage_a_adapter_error:{type(exc).__name__}",),
            observations,
            mutation_attempted=True,
            orders_submitted=orders_submitted,
        )

    fill_count = _count(observation, "fill_count", "fills")
    resting_verified = observation.get("resting_verified") is True or str(
        observation.get("status", "")
    ).upper() in {"RESTING", "OPEN", "ACTIVE"}
    try:
        after_account = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        after_account = {}
        blockers.append(f"stage_a_account_read_error:{type(exc).__name__}")
    position_after = _position_from_account(after_account)
    if position_after is None:
        position_after = _decimal(observation.get("position_after"))
    active_before_cancel = _count_value(after_account, "active_orders")

    unexpected_fill = fill_count is None or fill_count > 0 or (
        position_after is not None and position_after != ZERO
    )
    if unexpected_fill:
        blockers.append("ABORT_STAGE_A_UNEXPECTED_FILL")

    # A fill can race a cancellation. Always cancel the remaining order before
    # the authoritative post-order account read; never submit a flattening
    # order from Stage A.
    cancel_acknowledged, cancel_error = _cancel_once(
        adapter, trading_pair, str(native_order_id)
    )
    orders_cancelled = 1
    if cancel_error:
        blockers.append(cancel_error)

    try:
        final_account = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        final_account = after_account
        blockers.append(f"stage_a_final_account_read_error:{type(exc).__name__}")
    final_position = _position_from_account(final_account)
    final_active_orders = _count_value(final_account, "active_orders")
    if unexpected_fill:
        cleanup_blockers = validate_account_clean(final_account)
        if cleanup_blockers:
            blockers.append("MAINNET_LIFECYCLE_CLEANUP_REQUIRED")
            blockers.extend(cleanup_blockers)
    stage_result = {
        "status": "PASS",
        "position_action": plan.position_action,
        "reduce_only": plan.reduce_only,
        "order_type": plan.order_type,
        "time_in_force": plan.time_in_force,
        "post_only_requested": plan.time_in_force == "post_only" and plan.passive,
        "resting_verified": resting_verified,
        "native_order_id": str(native_order_id),
        "fill_count": fill_count,
        "cancel_acknowledged": cancel_acknowledged or final_active_orders == 0,
        "active_orders": final_active_orders,
        "position_after": final_position,
    }
    blockers.extend(validate_stage_a_result(stage_result))
    observations.update(
        {
            "native_order_id": str(native_order_id),
            "submitted_plan": plan.as_dict(),
            "order_observation": observation,
            "account_after_submit": after_account,
            "account_after_cancel": final_account,
            "active_orders_before_cancel": active_before_cancel,
            "cancel_acknowledged": stage_result["cancel_acknowledged"],
            "cleanup_required": bool(unexpected_fill and validate_account_clean(final_account)),
        }
    )
    return LifecycleExecutionResult(
        CanaryStage.STAGE_A,
        "PASS" if not blockers else "FAIL",
        tuple(dict.fromkeys(str(item) for item in blockers)),
        observations,
        mutation_attempted=True,
        orders_submitted=orders_submitted,
        orders_cancelled=orders_cancelled,
        fills=max(fill_count or 0, 0),
        position_changes=int(final_position not in (None, ZERO)),
    )


def run_stage_b(
    adapter: LifecycleAdapter,
    *,
    trading_pair: str,
    open_plan: DryRunPlan,
    close_plan: DryRunPlan,
    preflight: Mapping[str, Any],
    authorization: AuthorizationDecision,
    timeout_seconds: float = 60.0,
) -> LifecycleExecutionResult:
    """Open the minimum position, reconcile it, reduce-only close it, and clean up."""

    blockers = list(_execution_preflight_blockers(preflight, side=open_plan.side or "BUY"))
    blockers.extend(open_plan.blockers)
    blockers.extend(close_plan.blockers)
    if not authorization.mutation_allowed:
        blockers.extend(authorization.blockers or ("authorization_denied",))
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "BLOCKED",
            tuple(dict.fromkeys(blockers)),
            {"authorization": authorization.as_dict()},
        )
    if blockers:
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "BLOCKED",
            tuple(dict.fromkeys(blockers)),
            {"authorization": authorization.as_dict()},
        )

    observations: dict[str, Any] = {"authorization": authorization.as_dict()}
    orders_submitted = 0
    orders_cancelled = 0
    fills = 0
    position_changes = 0
    try:
        initial_account = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "FAIL",
            (f"stage_b_initial_account_read_error:{type(exc).__name__}",),
            observations,
        )
    initial_clean_blockers = validate_account_clean(initial_account)
    if initial_clean_blockers:
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "BLOCKED",
            tuple(initial_clean_blockers),
            {"authorization": authorization.as_dict(), "initial_account": initial_account},
        )

    try:
        open_order_id = adapter.submit_open(trading_pair, open_plan)
        orders_submitted += 1
        open_observation = dict(
            adapter.wait_for_order(trading_pair, str(open_order_id), timeout_seconds)
        )
        account_after_open = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "FAIL",
            (f"stage_b_open_adapter_error:{type(exc).__name__}",),
            observations,
            mutation_attempted=True,
            orders_submitted=orders_submitted,
        )

    open_fill_count = _count(open_observation, "fill_count", "fills")
    fills += max(open_fill_count or 0, 0)
    position_after_open = _position_from_account(account_after_open)
    if position_after_open is None:
        position_after_open = _decimal(open_observation.get("position_after"))
    expected_sign = Decimal("1") if str(open_plan.side).upper() == "BUY" else Decimal("-1")
    if open_fill_count is None or open_fill_count < 1:
        blockers.append("stage_b_open_fill_not_reconciled")
    if position_after_open is None or position_after_open == ZERO:
        blockers.append("stage_b_position_not_opened")
    elif position_after_open * expected_sign <= ZERO:
        blockers.append("stage_b_open_side_mismatch")
    else:
        position_changes += 1
    if blockers:
        cancelled, cancel_error = _cancel_once(adapter, trading_pair, str(open_order_id))
        orders_cancelled += 1
        if cancel_error:
            blockers.append(cancel_error)
        observations.update(
            {
                "open_order_id": str(open_order_id),
                "open_plan": open_plan.as_dict(),
                "open_observation": open_observation,
                "initial_account": initial_account,
                "account_after_open": account_after_open,
                "open_cancel_acknowledged": cancelled,
            }
        )
        return LifecycleExecutionResult(
            CanaryStage.STAGE_B,
            "FAIL",
            tuple(dict.fromkeys(blockers)),
            observations,
            mutation_attempted=True,
            orders_submitted=orders_submitted,
            orders_cancelled=orders_cancelled,
            fills=fills,
            position_changes=position_changes,
        )

    close_amount = abs(position_after_open)
    close_request = replace(
        close_plan,
        amount_base=close_amount,
        notional_quote=(close_amount * close_plan.price if close_plan.price else None),
    )
    try:
        close_order_id = adapter.submit_close(trading_pair, close_request)
        orders_submitted += 1
        close_observation = dict(
            adapter.wait_for_order(trading_pair, str(close_order_id), timeout_seconds)
        )
        account_after_close = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        blockers.append(f"stage_b_close_adapter_error:{type(exc).__name__}")
        account_after_close = account_after_open
        close_order_id = None
        close_observation = {}

    close_fill_count = _count(close_observation, "fill_count", "fills")
    fills += max(close_fill_count or 0, 0)
    position_after_close = _position_from_account(account_after_close)
    if position_after_close is None:
        position_after_close = _decimal(close_observation.get("position_after"))
    if position_after_close not in (None, position_after_open):
        position_changes += 1
    close_result = {
        "status": "PASS",
        "position_action": "CLOSE",
        "reduce_only": close_request.reduce_only,
        "order_type": close_request.order_type,
        "time_in_force": close_request.time_in_force,
        "close_reduce_only_requested": close_request.reduce_only,
        "close_native_order_id": None if close_order_id is None else str(close_order_id),
        "fill_count": close_fill_count,
        "position_before": position_after_open,
        "position_after": position_after_close,
        "close_position_before": position_after_open,
        "close_position_after": position_after_close,
        "oversized_close_tested": False,
    }
    blockers.extend(validate_stage_b_result(close_result))
    close_transition = validate_position_transition(
        position_after_open,
        position_after_close,
        action="CLOSE",
        reduce_only=True,
    )
    blockers.extend(close_transition)

    try:
        cleanup_account = dict(adapter.read_account(trading_pair))
    except Exception as exc:  # pragma: no cover - adapter-specific failure
        cleanup_account = account_after_close
        blockers.append(f"stage_b_cleanup_account_read_error:{type(exc).__name__}")
    cleanup = {
        "status": "PASS",
        "position_zero": (_position_from_account(cleanup_account) or ZERO) == ZERO,
        "active_orders": _count_value(cleanup_account, "active_orders"),
        "managed_executors": _count_value(cleanup_account, "managed_executors"),
        "unmanaged_executors": _count_value(cleanup_account, "unmanaged_executors"),
    }
    cleanup_blockers = validate_cleanup(cleanup)
    blockers.extend(cleanup_blockers)
    observations.update(
        {
            "initial_account": initial_account,
            "open_order_id": str(open_order_id),
            "open_plan": open_plan.as_dict(),
            "open_observation": open_observation,
            "account_after_open": account_after_open,
            "close_order_id": close_result["close_native_order_id"],
            "close_plan": close_request.as_dict(),
            "close_observation": close_observation,
            "account_after_close": account_after_close,
            "cleanup_account": cleanup_account,
            "cleanup": cleanup,
        }
    )
    return LifecycleExecutionResult(
        CanaryStage.STAGE_B,
        "PASS" if not blockers else "FAIL",
        tuple(dict.fromkeys(str(item) for item in blockers)),
        observations,
        mutation_attempted=True,
        orders_submitted=orders_submitted,
        orders_cancelled=orders_cancelled,
        fills=fills,
        position_changes=position_changes,
    )


def build_lifecycle_evidence_document(
    *,
    source: Mapping[str, Any],
    runtime: Mapping[str, Any],
    account: Mapping[str, Any],
    stage_a: Mapping[str, Any],
    stage_b: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    static_contract: Mapping[str, Any] | None = None,
    evidence_id: str,
    created_at: str,
) -> LifecycleEvidence:
    """Build the full evidence shape; validation remains a separate step."""

    cleanup_blockers = validate_cleanup(cleanup)
    stage_a_blockers = validate_stage_a_result(stage_a)
    stage_b_blockers = validate_stage_b_result(stage_b)
    payload = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "created_at": created_at,
        **EXPECTED_IDENTITY,
        "source": dict(source),
        "runtime": dict(runtime),
        "account": dict(account),
        "static_contract": dict(static_contract or EXPECTED_STATIC_CONTRACT),
        "stage_a": {**dict(stage_a), "blockers": list(stage_a_blockers)},
        "stage_b": {**dict(stage_b), "blockers": list(stage_b_blockers)},
        "cleanup": {**dict(cleanup), "blockers": list(cleanup_blockers)},
        "verdict": {
            "status": "PASS"
            if not stage_a_blockers and not stage_b_blockers and not cleanup_blockers
            else "FAIL",
            "blockers": list(
                dict.fromkeys([*stage_a_blockers, *stage_b_blockers, *cleanup_blockers])
            ),
        },
    }
    return build_lifecycle_evidence(payload)


def build_dry_run_result(
    snapshot: Mapping[str, Any],
    *,
    now: float | None = None,
    side: str = "BUY",
    stage_a_ticks_behind_touch: int = 5,
    stage_a_maker_guard_ticks: int | None = None,
    stage_a_maker_guard_bps: Decimal = Decimal("15"),
) -> DryRunResult:
    """Calculate both proposed stages using only supplied read-only data."""

    runtime = _mapping(snapshot.get("runtime"))
    account = _mapping(snapshot.get("account"))
    bbo = _mapping(snapshot.get("bbo"))
    rules = _mapping(snapshot.get("rules"))
    static_contract = _mapping(snapshot.get("static_contract"))
    blockers = [
        *validate_market_identity(_mapping(snapshot.get("identity"))),
        *validate_runtime_identity(runtime, _mapping(snapshot.get("expected_runtime"))),
        *validate_preflight_runtime_binding(snapshot),
        *validate_static_contract(static_contract),
        *validate_account_clean(account),
        *validate_market_freshness(
            bbo, now=now, max_age_seconds=float(snapshot.get("max_bbo_age_seconds", 10.0))
        ),
    ]
    stage_a = build_stage_a_plan(
        side=side,
        bbo=bbo,
        rules=rules,
        ticks_behind_touch=stage_a_ticks_behind_touch,
        maker_guard_ticks=stage_a_maker_guard_ticks,
        maker_guard_bps=stage_a_maker_guard_bps,
    )
    stage_b = build_stage_b_plan(side=side, bbo=bbo, rules=rules)
    blockers.extend(stage_a.blockers)
    auth = evaluate_authorization(CanaryMode.DRY_RUN)
    return DryRunResult(
        mode=CanaryMode.DRY_RUN,
        preflight_blockers=tuple(dict.fromkeys(blockers)),
        authorization=auth,
        stage_a_plan=stage_a,
        stage_b_plan=stage_b,
    )


__all__ = [
    "AuthorizationDecision",
    "CanaryMode",
    "CanaryStage",
    "DryRunPlan",
    "DryRunResult",
    "LifecycleAdapter",
    "LifecycleExecutionResult",
    "build_dry_run_result",
    "build_lifecycle_evidence_document",
    "build_stage_a_plan",
    "build_stage_b_plan",
    "evaluate_authorization",
    "minimum_safe_amount",
    "maker_guard_distance",
    "passive_price",
    "validate_maker_price",
    "validate_account_clean",
    "validate_market_freshness",
    "validate_market_identity",
    "validate_preflight_runtime_binding",
    "validate_position_transition",
    "validate_stage_a_result",
    "validate_stage_b_result",
    "validate_static_contract",
    "validate_cleanup",
    "validate_runtime_identity",
    "run_stage_a",
    "run_stage_b",
]
