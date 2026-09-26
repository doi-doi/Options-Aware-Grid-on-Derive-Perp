#!/usr/bin/env python3
# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""Gated Derive Stage B open/close canary using only Hummingbot APIs.

The installed configuration stays in ``DRY_RUN``. A real run requires an
operator confirmation, an exact Stage B authorization, a one-time token hash,
and the SHA-256 of a passed Stage A result. It opens only the minimum valid
amount with a bounded GTC LIMIT, then closes the observed position with a
bounded GTC LIMIT and ``PositionAction.CLOSE``. It never uses REST, signing,
or an automatic market-order fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
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
    build_hummingbot_runtime_snapshot,
)
from derive_options_adaptive_grid.lifecycle_canary import (  # noqa: E402
    CanaryMode,
    CanaryStage,
    build_dry_run_result,
    evaluate_authorization,
    minimum_safe_amount,
    validate_cleanup,
    validate_stage_b_result,
)

DATA_ROOT = Path("/home/hummingbot/data/derive_options_grid_lifecycle")
STAGE_A_RESULT_PATH = DATA_ROOT / "stage_a.json"
STAGE_B_RESULT_PATH = DATA_ROOT / "stage_b.json"


class DeriveOptionsGridStageBConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: list[str] = []
    exchange: str = Field("derive_perpetual")
    trading_pair: str = Field("SOL-USDC")
    mode: str = Field("DRY_RUN")
    open_side: str = Field("BUY")
    operator_confirmed: bool = Field(False)
    stage_authorization: str = Field("NONE")
    one_run_token: str = Field("")
    one_run_token_sha256: str = Field("")
    stage_a_result_sha256: str = Field("")
    max_bbo_age_seconds: float = Field(10.0, gt=0)
    max_open_slippage_bps: Decimal = Field(Decimal("10"), ge=Decimal("0"), le=Decimal("100"))
    max_close_slippage_bps: Decimal = Field(Decimal("20"), ge=Decimal("0"), le=Decimal("100"))
    open_fill_timeout_seconds: float = Field(30.0, gt=0)
    cancel_timeout_seconds: float = Field(30.0, gt=0)
    close_fill_timeout_seconds: float = Field(45.0, gt=0)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.exchange] = markets.get(self.exchange, set()) | {self.trading_pair}
        return markets


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _bounded_price(
    *, side: str, bid: Decimal, ask: Decimal, increment: Decimal, bps: Decimal
) -> Decimal | None:
    if bid <= 0 or ask <= bid or increment <= 0:
        return None
    multiplier = Decimal("1") + (bps / Decimal("10000"))
    if side == "BUY":
        return ((ask * multiplier) / increment).to_integral_value(ROUND_CEILING) * increment
    if side == "SELL":
        return ((bid / multiplier) / increment).to_integral_value(ROUND_FLOOR) * increment
    return None


class DeriveOptionsGridStageBCanary(StrategyV2Base):
    """Event-driven, minimum-size open and reduce-only close lifecycle."""

    def __init__(self, connectors: dict[str, ConnectorBase], config: DeriveOptionsGridStageBConfig):
        super().__init__(connectors, config)
        self.config = config
        self.phase = "INITIAL"
        self.deadline = 0.0
        self.snapshot: dict[str, Any] = {}
        self.open_order_id: str | None = None
        self.open_native_order_id: str | None = None
        self.close_order_id: str | None = None
        self.close_native_order_id: str | None = None
        self.open_fills = 0
        self.close_fills = 0
        self.open_cancel_requested = False
        self.close_cancel_requested = False
        self.position_before_close: Decimal | None = None

    @property
    def connector(self) -> ConnectorBase:
        return self.connectors[self.config.exchange]

    def on_tick(self):
        if self.phase == "FINISHED":
            return
        if self.phase == "INITIAL":
            self._prepare()
        elif self.phase == "VERIFY_OPEN":
            self._start_close_from_account()
        elif self.phase == "VERIFY_CLOSE":
            self._finalize()
        elif time.time() > self.deadline:
            self._on_timeout()

    def _prepare(self) -> None:
        try:
            self.snapshot = build_hummingbot_runtime_snapshot(
                self.connector, trading_pair=self.config.trading_pair
            )
            self.snapshot["max_bbo_age_seconds"] = self.config.max_bbo_age_seconds
            dry_run = build_dry_run_result(self.snapshot, side=self.config.open_side.upper())
        except Exception as exc:
            self._finish("FAIL", [f"preflight_capture_error:{type(exc).__name__}"])
            return
        if self.config.mode.upper() == CanaryMode.DRY_RUN.value:
            self._finish("DRY_RUN", list(dry_run.preflight_blockers), dry_run=dry_run.as_dict())
            return
        if self.config.mode.upper() != CanaryMode.STAGE_B.value:
            self._finish("BLOCKED", ["invalid_canary_mode"], dry_run=dry_run.as_dict())
            return
        stage_a_blockers = self._validate_stage_a_result(self.snapshot)
        authorization = self._authorization(not dry_run.preflight_blockers, not stage_a_blockers)
        blockers = [*dry_run.preflight_blockers, *stage_a_blockers, *authorization.blockers]
        if blockers:
            self._finish("BLOCKED", blockers, dry_run=dry_run.as_dict())
            return
        self._submit_open()

    def _validate_stage_a_result(self, current_snapshot: dict[str, Any]) -> list[str]:
        try:
            raw = STAGE_A_RESULT_PATH.read_bytes()
            result = json.loads(raw)
        except Exception:
            return ["stage_a_result_unavailable"]
        digest = hashlib.sha256(raw).hexdigest()
        if not self.config.stage_a_result_sha256 or digest != self.config.stage_a_result_sha256:
            return ["stage_a_result_sha256_missing_or_wrong"]
        blockers = list(result.get("blockers", ()) or ())
        if str(result.get("status", "")).upper() != "PASS":
            blockers.append("stage_a_result_not_pass")
        if result.get("stage") != "STAGE_A":
            blockers.append("stage_a_result_wrong_stage")
        stage_a_runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
        current_runtime = current_snapshot.get("runtime", {})
        for key in (
            "base_image_digest",
            "runtime_contract_id",
            "connector_source_sha256",
            "fee_source_sha256",
            "runtime_image_ref",
        ):
            if not stage_a_runtime.get(key) or stage_a_runtime.get(key) != current_runtime.get(key):
                blockers.append(f"stage_a_runtime_mismatch:{key}")
        stage_a_account = result.get("account") if isinstance(result.get("account"), dict) else {}
        if stage_a_account.get("account_fingerprint") != current_snapshot["account"].get(
            "account_fingerprint"
        ):
            blockers.append("stage_a_account_fingerprint_mismatch")
        return list(dict.fromkeys(str(item) for item in blockers))

    def _authorization(self, runtime_valid: bool, stage_a_passed: bool):
        digest = hashlib.sha256(self.config.one_run_token.encode("utf-8")).hexdigest()
        token_valid = (
            bool(self.config.one_run_token_sha256)
            and digest == self.config.one_run_token_sha256
        )
        return evaluate_authorization(
            CanaryMode.STAGE_B,
            CanaryStage.STAGE_B,
            cli_authorized=self.config.operator_confirmed and token_valid,
            runtime_stage_authorization=self.config.stage_authorization,
            one_run_token=self.config.one_run_token if token_valid else None,
            expected_one_run_token=self.config.one_run_token if token_valid else None,
            runtime_valid=runtime_valid,
            stage_a_passed=stage_a_passed,
        )

    def _order_values(self, side: str, bps: Decimal) -> tuple[Decimal, Decimal] | None:
        bbo = self.snapshot["bbo"]
        rules = self.snapshot["rules"]
        bid = _decimal(bbo.get("best_bid"))
        ask = _decimal(bbo.get("best_ask"))
        increment = _decimal(rules.get("price_increment"))
        if bid is None or ask is None or increment is None:
            return None
        price = _bounded_price(side=side, bid=bid, ask=ask, increment=increment, bps=bps)
        amount = minimum_safe_amount(rules, price) if price else None
        return (amount, price) if amount is not None and price is not None else None

    def _submit_open(self) -> None:
        side = self.config.open_side.upper()
        values = self._order_values(side, self.config.max_open_slippage_bps)
        if values is None:
            self._finish("BLOCKED", ["bounded_open_plan_unavailable"])
            return
        amount, price = values
        method = self.buy if side == "BUY" else self.sell
        try:
            self.open_order_id = method(
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                amount=amount,
                order_type=OrderType.LIMIT,
                price=price,
                position_action=PositionAction.OPEN,
            )
        except Exception as exc:
            self._finish("FAIL", [f"stage_b_open_submit_error:{type(exc).__name__}"])
            return
        self.phase = "WAIT_OPEN_FILL"
        self.deadline = time.time() + self.config.open_fill_timeout_seconds

    def _on_created(self, order_id: str, native_order_id: str | None) -> None:
        if order_id == self.open_order_id:
            self.open_native_order_id = native_order_id
        elif order_id == self.close_order_id:
            self.close_native_order_id = native_order_id

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        self._on_created(event.order_id, event.exchange_order_id)

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        self._on_created(event.order_id, event.exchange_order_id)

    def did_fill_order(self, event: OrderFilledEvent):
        if event.order_id == self.open_order_id:
            self.open_fills += 1
        elif event.order_id == self.close_order_id:
            self.close_fills += 1

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        self._on_completed(event.order_id, event.exchange_order_id)

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        self._on_completed(event.order_id, event.exchange_order_id)

    def _on_completed(self, order_id: str, native_order_id: str | None) -> None:
        if order_id == self.open_order_id and self.phase == "WAIT_OPEN_FILL":
            self.open_native_order_id = self.open_native_order_id or native_order_id
            self.phase = "VERIFY_OPEN"
        elif order_id == self.close_order_id and self.phase == "WAIT_CLOSE_FILL":
            self.close_native_order_id = self.close_native_order_id or native_order_id
            self.phase = "VERIFY_CLOSE"

    def did_cancel_order(self, event: OrderCancelledEvent):
        if event.order_id == self.open_order_id and self.phase == "WAIT_OPEN_CANCEL":
            self.phase = "VERIFY_OPEN"
        elif event.order_id == self.close_order_id and self.phase == "WAIT_CLOSE_CANCEL":
            self.phase = "VERIFY_CLOSE"

    def did_fail_order(self, event: MarketOrderFailureEvent):
        if event.order_id in {self.open_order_id, self.close_order_id}:
            self._finish("FAIL", ["stage_b_order_failure"])

    def _on_timeout(self) -> None:
        if self.phase == "WAIT_OPEN_FILL" and self.open_order_id:
            try:
                self.cancel(self.config.exchange, self.config.trading_pair, self.open_order_id)
            except Exception as exc:
                self._finish("FAIL", [f"stage_b_open_cancel_error:{type(exc).__name__}"])
                return
            self.open_cancel_requested = True
            self.phase = "WAIT_OPEN_CANCEL"
            self.deadline = time.time() + self.config.cancel_timeout_seconds
        elif self.phase == "WAIT_CLOSE_FILL" and self.close_order_id:
            try:
                self.cancel(self.config.exchange, self.config.trading_pair, self.close_order_id)
            except Exception as exc:
                self._finish("FAIL", [f"stage_b_close_cancel_error:{type(exc).__name__}"])
                return
            self.close_cancel_requested = True
            self.phase = "WAIT_CLOSE_CANCEL"
            self.deadline = time.time() + self.config.cancel_timeout_seconds
        else:
            self._finish("FAIL", [f"stage_b_timeout:{self.phase.lower()}"])

    def _start_close_from_account(self) -> None:
        try:
            self.snapshot = build_hummingbot_runtime_snapshot(
                self.connector, trading_pair=self.config.trading_pair
            )
            position = _decimal(self.snapshot["account"].get("position_base"))
        except Exception as exc:
            self._finish("FAIL", [f"stage_b_open_reconciliation_error:{type(exc).__name__}"])
            return
        if position is None or position == 0:
            self._finish(
                "FAIL", ["stage_b_open_position_not_observed"], account=self.snapshot["account"]
            )
            return
        if not self.open_native_order_id or self.open_fills < 1:
            self._finish(
                "FAIL",
                ["stage_b_open_fill_or_native_id_missing"],
                account=self.snapshot["account"],
            )
            return
        side = "SELL" if position > 0 else "BUY"
        values = self._order_values(side, self.config.max_close_slippage_bps)
        if values is None:
            self._finish(
                "FAIL", ["bounded_close_plan_unavailable"], account=self.snapshot["account"]
            )
            return
        _, price = values
        self.position_before_close = position
        method = self.buy if side == "BUY" else self.sell
        try:
            self.close_order_id = method(
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                amount=abs(position),
                order_type=OrderType.LIMIT,
                price=price,
                position_action=PositionAction.CLOSE,
            )
        except Exception as exc:
            self._finish("FAIL", [f"stage_b_close_submit_error:{type(exc).__name__}"])
            return
        self.phase = "WAIT_CLOSE_FILL"
        self.deadline = time.time() + self.config.close_fill_timeout_seconds

    def _finalize(self) -> None:
        try:
            final = build_hummingbot_runtime_snapshot(
                self.connector, trading_pair=self.config.trading_pair
            )["account"]
        except Exception as exc:
            self._finish("FAIL", [f"stage_b_final_reconciliation_error:{type(exc).__name__}"])
            return
        position_after = final.get("position_base")
        stage_b = {
            "status": "PASS",
            "position_action": "CLOSE",
            "reduce_only": True,
            "order_type": "LIMIT",
            "time_in_force": "gtc",
            "close_reduce_only_requested": True,
            "close_native_order_id": self.close_native_order_id,
            "fill_count": self.close_fills,
            "position_before": self.position_before_close,
            "position_after": position_after,
        }
        cleanup = {
            "position_zero": _decimal(position_after) == 0,
            "active_orders": final.get("active_orders"),
            "managed_executors": final.get("managed_executors"),
            "unmanaged_executors": final.get("unmanaged_executors"),
        }
        blockers = [*validate_stage_b_result(stage_b), *validate_cleanup(cleanup)]
        self._finish(
            "PASS" if not blockers else "FAIL",
            blockers,
            stage_b=stage_b,
            cleanup=cleanup,
            account=final,
        )

    def _finish(self, status: str, blockers: list[str], **extra: Any) -> None:
        if self.phase == "FINISHED":
            return
        self.phase = "FINISHED"
        result = {
            "schema_version": 1,
            "stage": "STAGE_B",
            "status": status,
            "blockers": list(dict.fromkeys(str(item) for item in blockers)),
            "mutation_attempted": (
                self.open_order_id is not None or self.close_order_id is not None
            ),
            "orders_submitted": (
                int(self.open_order_id is not None) + int(self.close_order_id is not None)
            ),
            "orders_cancelled": int(self.open_cancel_requested) + int(self.close_cancel_requested),
            "open_fills": self.open_fills,
            "close_fills": self.close_fills,
            "runtime": self.snapshot.get("runtime", {}),
            "identity": self.snapshot.get("identity", {}),
            **extra,
        }
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        STAGE_B_RESULT_PATH.write_text(
            json.dumps(result, sort_keys=True, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        self.log_with_clock(logging.INFO, f"Derive Stage B canary finished: {status}")
        HummingbotApplication.main_application().stop()
