#!/usr/bin/env python3
# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""Gated Hummingbot-native Stage A canary for Derive SOL-USDC.

The default configuration is entirely read-only. A Stage A run additionally
requires an explicit operator flag, the exact ``STAGE_A`` authorization string,
and a one-time token matching the configured SHA-256 digest. The token is
never logged or written to the result file. Orders, when separately enabled,
use only Hummingbot's connector methods and are observed through Hummingbot
events; this script has no REST client, signer, or exchange-specific transport.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction
from hummingbot.core.event.events import (
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCreatedEvent,
)
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from pydantic import Field

SUPPORT_ROOT = Path(
    "/home/hummingbot/controllers/market_making/derive_options_adaptive_grid_support"
)
if SUPPORT_ROOT.exists() and str(SUPPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPPORT_ROOT))

from derive_options_adaptive_grid.connector_contract import (  # noqa: E402
    OrderBookFreshnessTracker,
    build_hummingbot_runtime_snapshot,
)
from derive_options_adaptive_grid.lifecycle_canary import (  # noqa: E402
    CanaryMode,
    CanaryStage,
    build_dry_run_result,
    evaluate_authorization,
    validate_account_clean,
    validate_stage_a_result,
)

RESULT_PATH = Path("/home/hummingbot/data/derive_options_grid_lifecycle/stage_a.json")


class DeriveOptionsGridStageAConfig(StrategyV2ConfigBase):
    """Configuration deliberately defaults to a no-mutation execution path."""

    script_file_name: str = os.path.basename(__file__)
    controllers_config: list[str] = []
    exchange: str = Field("derive_perpetual")
    trading_pair: str = Field("SOL-USDC")
    mode: str = Field("DRY_RUN")
    side: str = Field("BUY")
    operator_confirmed: bool = Field(False)
    stage_authorization: str = Field("NONE")
    one_run_token: str = Field("")
    one_run_token_sha256: str = Field("")
    max_bbo_age_seconds: float = Field(10.0, gt=0)
    # Stage A is a cancellation probe, not a fill attempt. Keep a conservative
    # guard and re-plan from a new order-book marker immediately before the
    # one allowed submit request. This guard is Stage-A-only; it does not alter
    # adaptive-grid pricing.
    stage_a_maker_guard_ticks: int = Field(5, ge=3, le=100)
    stage_a_maker_guard_bps: Decimal = Field(Decimal("15"), ge=0, le=1000)
    create_timeout_seconds: float = Field(30.0, gt=0)
    cancel_timeout_seconds: float = Field(30.0, gt=0)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.exchange] = markets.get(self.exchange, set()) | {self.trading_pair}
        return markets


class DeriveOptionsGridStageACanary(StrategyV2Base):
    """One passive open order, then a single cancellation, or a read-only exit."""

    def __init__(
        self, connectors: dict[str, ConnectorBase], config: DeriveOptionsGridStageAConfig
    ):
        super().__init__(connectors, config)
        self.config = config
        self._phase = "INITIAL"
        self._deadline = 0.0
        self._client_order_id: str | None = None
        self._native_order_id: str | None = None
        self._fill_count = 0
        self._cancel_requested = False
        self._resting_verified = False
        self._abort_blockers: list[str] = []
        self._snapshot: dict[str, Any] = {}
        self._submission_snapshot: dict[str, Any] = {}
        self._submission_dry_run: dict[str, Any] = {}
        self._plan: Any | None = None
        self._book_freshness = OrderBookFreshnessTracker()
        self._startup_marker: tuple[Any, Any] | None = None
        self._failure_observed = False
        self._failure_details: dict[str, str] = {}

    @property
    def connector(self) -> ConnectorBase:
        return self.connectors[self.config.exchange]

    def on_tick(self):
        if self._phase == "FINISHED":
            return
        if self._phase == "INITIAL":
            self._prepare()
        elif self._phase == "WAITING_FOR_FRESH_MARKET":
            ready = self._try_submit_from_fresh_market()
            if (
                not ready
                and self._phase != "FINISHED"
                and time.time() > self._deadline
            ):
                blockers = ["stage_a_fresh_order_book_timeout"]
                blockers.extend(self._submission_dry_run.get("preflight_blockers", ()))
                self._finish(
                    "BLOCKED",
                    blockers,
                    dry_run=self._submission_dry_run,
                    submission_snapshot=self._submission_snapshot,
                )
        elif self._phase == "WAITING_FOR_CREATE" and time.time() > self._deadline:
            self._abort_and_reconcile(["stage_a_create_timeout"])
        elif self._phase == "WAITING_FOR_RESTING":
            if self._order_tracker_reports_resting():
                self._resting_verified = True
                self._request_stage_a_cancel()
            elif time.time() > self._deadline:
                self._abort_and_reconcile(["stage_a_resting_not_verified"])
        elif self._phase == "WAITING_FOR_CANCEL" and time.time() > self._deadline:
            self._abort_and_reconcile(["stage_a_cancel_timeout"])
        elif self._phase == "WAITING_FOR_ABORT_CANCEL" and time.time() > self._deadline:
            self._abort_reconcile(["stage_a_abort_cancel_timeout"])
        elif self._phase == "FINALIZE":
            self._finalize_stage_a()

    def _prepare(self) -> None:
        try:
            self._snapshot = build_hummingbot_runtime_snapshot(
                self.connector,
                trading_pair=self.config.trading_pair,
                freshness_tracker=self._book_freshness,
            )
            self._snapshot["max_bbo_age_seconds"] = self.config.max_bbo_age_seconds
            dry_run = build_dry_run_result(
                self._snapshot,
                side=self.config.side.upper(),
                stage_a_maker_guard_ticks=self.config.stage_a_maker_guard_ticks,
                stage_a_maker_guard_bps=self.config.stage_a_maker_guard_bps,
            )
        except Exception as exc:
            self._finish("FAIL", [f"preflight_capture_error:{type(exc).__name__}"])
            return

        mode = self._parse_mode()
        if mode is None:
            self._finish("BLOCKED", ["invalid_canary_mode"], dry_run=dry_run.as_dict())
            return
        if mode is CanaryMode.DRY_RUN:
            self._finish(
                "DRY_RUN",
                list(dry_run.preflight_blockers),
                dry_run=dry_run.as_dict(),
                preflight_snapshot=self._snapshot,
            )
            return
        if mode is not CanaryMode.STAGE_A:
            self._finish(
                "BLOCKED", ["stage_b_requires_separate_review"], dry_run=dry_run.as_dict()
            )
            return

        authorization = self._authorization(not dry_run.preflight_blockers)
        blockers = list(dry_run.preflight_blockers) + list(authorization.blockers)
        self._plan = dry_run.stage_a_plan
        blockers.extend(self._plan.blockers)
        if blockers:
            self._finish("BLOCKED", blockers, dry_run=dry_run.as_dict())
            return
        self._startup_marker = self._snapshot.get("bbo", {}).get("order_book_marker")
        self._phase = "WAITING_FOR_FRESH_MARKET"
        self._deadline = time.time() + self.config.create_timeout_seconds
        self._try_submit_from_fresh_market()

    def _parse_mode(self) -> CanaryMode | None:
        try:
            return CanaryMode(self.config.mode.upper())
        except ValueError:
            return None

    def _authorization(self, runtime_valid: bool):
        digest = hashlib.sha256(self.config.one_run_token.encode("utf-8")).hexdigest()
        token_valid = (
            bool(self.config.one_run_token_sha256)
            and digest == self.config.one_run_token_sha256
        )
        return evaluate_authorization(
            CanaryMode.STAGE_A,
            CanaryStage.STAGE_A,
            cli_authorized=self.config.operator_confirmed and token_valid,
            runtime_stage_authorization=self.config.stage_authorization,
            one_run_token=self.config.one_run_token if token_valid else None,
            expected_one_run_token=self.config.one_run_token if token_valid else None,
            runtime_valid=runtime_valid,
        )

    def _submit_stage_a(self) -> None:
        assert self._plan is not None
        method = self.buy if self._plan.side == "BUY" else self.sell
        try:
            self._client_order_id = method(
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                amount=self._plan.amount_base,
                order_type=OrderType.LIMIT_MAKER,
                price=self._plan.price,
                position_action=PositionAction.OPEN,
            )
        except Exception as exc:
            self._finish("FAIL", [f"stage_a_submit_error:{type(exc).__name__}"])
            return
        self._phase = "WAITING_FOR_CREATE"
        self._deadline = time.time() + self.config.create_timeout_seconds

    def _try_submit_from_fresh_market(self) -> bool:
        ready = self._refresh_submission_plan()
        if ready is True:
            self._submit_stage_a()
            return True
        return False

    def _refresh_submission_plan(self) -> bool | None:
        """Re-read the bound runtime and wait for a new order-book marker.

        The initial preflight is intentionally not reused as an execution
        quote. A fast BBO can make a previously passive price cross before the
        request reaches Derive, so the single allowed request must be built
        from a new connector update marker and the Stage-A-only maker guard.

        ``False`` means the connector has not produced a new marker yet and
        the caller may wait. ``None`` means a hard blocker was recorded.
        """

        try:
            snapshot = build_hummingbot_runtime_snapshot(
                self.connector,
                trading_pair=self.config.trading_pair,
                freshness_tracker=self._book_freshness,
            )
            snapshot["max_bbo_age_seconds"] = self.config.max_bbo_age_seconds
            dry_run = build_dry_run_result(
                snapshot,
                side=self.config.side.upper(),
                stage_a_maker_guard_ticks=self.config.stage_a_maker_guard_ticks,
                stage_a_maker_guard_bps=self.config.stage_a_maker_guard_bps,
            )
        except Exception as exc:
            blockers = (f"stage_a_submit_preflight_error:{type(exc).__name__}",)
            self._finish("BLOCKED", list(blockers))
            return None

        blockers = list(dry_run.preflight_blockers)
        initial_runtime = self._snapshot.get("runtime", {})
        current_runtime = snapshot.get("runtime", {})
        for key in ("runtime_instance_id", "runtime_contract_id", "runtime_image_ref"):
            if current_runtime.get(key) != initial_runtime.get(key):
                blockers.append(f"stage_a_submit_runtime_changed:{key}")
        self._submission_snapshot = snapshot
        self._submission_dry_run = dry_run.as_dict()
        marker = snapshot.get("bbo", {}).get("order_book_marker")
        if marker is None:
            self._finish(
                "BLOCKED",
                blockers or ["bbo_update_marker_unavailable"],
                dry_run=dry_run.as_dict(),
                submission_snapshot=snapshot,
            )
            return None
        if self._startup_marker is not None and marker == self._startup_marker:
            # A duplicate cached snapshot is not a fresh pre-submit market
            # observation. Keep waiting; the deadline turns this into an
            # explicit blocker without sending a stale quote.
            if any(item != "bbo_stale" for item in blockers):
                self._finish(
                    "BLOCKED",
                    blockers,
                    dry_run=dry_run.as_dict(),
                    submission_snapshot=snapshot,
                )
                return None
            return False

        if blockers:
            self._finish(
                "BLOCKED",
                blockers,
                dry_run=dry_run.as_dict(),
                submission_snapshot=snapshot,
            )
            return None

        # Use only this fresh plan for the request.  The initial plan remains
        # evidence of the first preflight, while this plan is the actual quote.
        self._plan = dry_run.stage_a_plan
        return True

    def _on_order_created(self, event: BuyOrderCreatedEvent | SellOrderCreatedEvent) -> None:
        if event.order_id != self._client_order_id or self._phase != "WAITING_FOR_CREATE":
            return
        self._native_order_id = event.exchange_order_id
        if not self._native_order_id:
            self._abort_and_reconcile(["stage_a_native_order_id_missing"])
            return
        self._phase = "WAITING_FOR_RESTING"
        self._deadline = time.time() + self.config.create_timeout_seconds

    def _order_tracker_reports_resting(self) -> bool:
        """Require an observed open order state before claiming a resting order."""

        orders = getattr(self.connector, "in_flight_orders", None)
        if not isinstance(orders, Mapping):
            return False
        order = orders.get(self._client_order_id)
        if order is None:
            return False
        state = getattr(order, "current_state", None)
        state_name = getattr(state, "name", None) or getattr(state, "value", None)
        if state_name is None:
            state_name = str(state)
        normalized_state = str(state_name).upper().rsplit(".", 1)[-1]
        return normalized_state in {"OPEN", "ACTIVE", "RESTING"}

    def _request_stage_a_cancel(self) -> None:
        if self._cancel_requested or self._phase == "FINISHED":
            return
        try:
            self.cancel(self.config.exchange, self.config.trading_pair, self._client_order_id)
            self._cancel_requested = True
        except Exception as exc:
            self._abort_and_reconcile([f"stage_a_cancel_error:{type(exc).__name__}"])
            return
        self._phase = "WAITING_FOR_CANCEL"
        self._deadline = time.time() + self.config.cancel_timeout_seconds

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        self._on_order_created(event)

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        self._on_order_created(event)

    def did_fill_order(self, event: OrderFilledEvent):
        if event.order_id == self._client_order_id:
            self._fill_count += 1
            native_order_id = getattr(event, "exchange_order_id", None)
            if native_order_id and not self._native_order_id:
                self._native_order_id = native_order_id
            self._abort_and_reconcile(["ABORT_STAGE_A_UNEXPECTED_FILL"])

    def did_fail_order(self, event: MarketOrderFailureEvent):
        if event.order_id == self._client_order_id:
            self._failure_observed = True
            details: dict[str, str] = {}
            for field_name in ("error_message", "error_type", "failure_reason", "message"):
                value = getattr(event, field_name, None)
                if value is not None and str(value).strip():
                    # Keep the exchange's failure classification useful for
                    # evidence while bounding it and never serializing the
                    # event object or connector credentials.
                    details[field_name] = str(value)[:500]
            if not details:
                details["event_type"] = type(event).__name__
            self._failure_details = details
            self._abort_and_reconcile(
                ["STAGE_A_REJECTED_BEFORE_EXCHANGE_ACCEPTANCE", "stage_a_order_failure"]
            )

    def did_cancel_order(self, event: OrderCancelledEvent):
        if event.order_id == self._client_order_id and self._phase == "WAITING_FOR_CANCEL":
            self._phase = "FINALIZE"
        elif (
            event.order_id == self._client_order_id
            and self._phase == "WAITING_FOR_ABORT_CANCEL"
        ):
            self._abort_reconcile()

    def _abort_and_reconcile(self, blockers: list[str]) -> None:
        """Cancel a possibly remaining order, then record account state.

        Stage A deliberately never places an offsetting order. If a fill
        happened, any non-zero position is surfaced as cleanup-required for a
        separately authorized operator action.
        """

        if self._phase == "FINISHED":
            return
        self._abort_blockers.extend(blockers)
        # A rejected post-only request has a client ID but no exchange/native
        # order.  There is nothing to cancel; reconcile immediately instead
        # of manufacturing a cancel timeout for a non-existent order.  Other
        # aborts (for example a create timeout) still attempt cancellation by
        # client ID because an accepted order may be awaiting its create event.
        if self._client_order_id is None or (
            self._native_order_id is None and self._failure_observed
        ):
            self._abort_reconcile()
            return
        if not self._cancel_requested:
            try:
                self.cancel(
                    self.config.exchange, self.config.trading_pair, self._client_order_id
                )
                self._cancel_requested = True
            except Exception as exc:
                self._abort_blockers.append(f"stage_a_abort_cancel_error:{type(exc).__name__}")
                self._abort_reconcile()
                return
        self._phase = "WAITING_FOR_ABORT_CANCEL"
        self._deadline = time.time() + self.config.cancel_timeout_seconds

    def _abort_reconcile(self, extra_blockers: list[str] | None = None) -> None:
        if self._phase == "FINISHED":
            return
        blockers = list(self._abort_blockers)
        blockers.extend(extra_blockers or [])
        try:
            account = build_hummingbot_runtime_snapshot(
                self.connector,
                trading_pair=self.config.trading_pair,
                freshness_tracker=self._book_freshness,
            )["account"]
        except Exception as exc:
            account = {}
            blockers.append(f"stage_a_abort_account_error:{type(exc).__name__}")
        account_blockers = validate_account_clean(account)
        if account_blockers:
            blockers.append("MAINNET_LIFECYCLE_CLEANUP_REQUIRED")
            blockers.extend(account_blockers)
        authoritative_fill_count = (
            0
            if self._failure_observed
            and self._native_order_id is None
            and not account_blockers
            else self._fill_count
        )
        if authoritative_fill_count == 0:
            self._fill_count = 0
        classification = (
            "REJECTED_BEFORE_ACCEPTANCE"
            if self._failure_observed and self._native_order_id is None
            else "ABORTED"
        )
        stage_a = {
            "status": "FAIL" if classification == "REJECTED_BEFORE_ACCEPTANCE" else "ABORTED",
            "classification": classification,
            "position_action": "OPEN",
            "reduce_only": False,
            "order_type": "LIMIT_MAKER",
            "time_in_force": "post_only",
            "post_only_requested": True,
            "native_order_id": self._native_order_id,
            "fill_count": authoritative_fill_count,
            "cancel_requested": self._cancel_requested,
            "exchange_order_accepted": self._native_order_id is not None,
            "active_orders": account.get("active_orders"),
            "position_after": account.get("position_base"),
            "cleanup_required": bool(account_blockers),
            "submitted_plan": None if self._plan is None else self._plan.as_dict(),
            "failure": self._failure_details or None,
        }
        self._finish(
            "FAIL",
            blockers,
            stage_a=stage_a,
            account=account,
            submission_snapshot=self._submission_snapshot,
            classification=classification,
            failure=self._failure_details or None,
        )

    def _finalize_stage_a(self) -> None:
        try:
            account = build_hummingbot_runtime_snapshot(
                self.connector,
                trading_pair=self.config.trading_pair,
                freshness_tracker=self._book_freshness,
            )["account"]
        except Exception as exc:
            self._finish("FAIL", [f"stage_a_final_account_error:{type(exc).__name__}"])
            return
        result = {
            "status": "PASS",
            "position_action": "OPEN",
            "reduce_only": False,
            "order_type": "LIMIT_MAKER",
            "time_in_force": "post_only",
            "post_only_requested": True,
            "resting_verified": self._resting_verified,
            "native_order_id": self._native_order_id,
            "fill_count": self._fill_count,
            "cancel_acknowledged": True,
            "exchange_order_accepted": self._native_order_id is not None,
            "active_orders": account.get("active_orders"),
            "position_after": account.get("position_base"),
            "submitted_plan": None if self._plan is None else self._plan.as_dict(),
        }
        self._finish(
            "PASS",
            list(validate_stage_a_result(result)),
            stage_a=result,
            account=account,
            submission_snapshot=self._submission_snapshot,
        )

    def _finish(self, status: str, blockers: list[str], **extra: Any) -> None:
        if self._phase == "FINISHED":
            return
        self._phase = "FINISHED"
        result = {
            "schema_version": 1,
            "stage": "STAGE_A",
            "status": status,
            "blockers": list(dict.fromkeys(str(item) for item in blockers)),
            "mutation_attempted": self._client_order_id is not None,
            "orders_submitted": int(self._native_order_id is not None),
            "orders_cancelled": int(self._cancel_requested),
            "fills": self._fill_count,
            "runtime": self._snapshot.get("runtime", {}),
            "identity": self._snapshot.get("identity", {}),
            "order_submit_attempts": int(self._client_order_id is not None),
            "exchange_order_accepted": self._native_order_id is not None,
            "classification": (
                "REJECTED_BEFORE_ACCEPTANCE"
                if self._failure_observed and self._native_order_id is None
                else None
            ),
            **extra,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(result, sort_keys=True, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        self.log_with_clock(logging.INFO, f"Derive Stage A canary finished: {status}")
        HummingbotApplication.main_application().stop()
