#!/usr/bin/env python3
"""Read-only default runner for the two-stage Derive mainnet canary.

The default command calculates the proposed Stage A and Stage B actions from
a supplied, already-captured Hummingbot snapshot.  It does not use REST, a
strategy-local Derive client, credentials, or an executor.  Future mutation
stages must be implemented through normal Hummingbot connector and executor
APIs and remain separately authorized.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any


def _add_repo_paths() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    candidates = (
        repo_root / "src",
        repo_root / "controllers/market_making/derive_options_adaptive_grid_support",
        Path("/home/hummingbot/controllers/market_making/derive_options_adaptive_grid_support"),
    )
    for path in reversed(candidates):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


class HummingbotNativeLifecycleAdapter:
    """Small adapter for future stages; every mutation stays in Hummingbot.

    The adapter intentionally exposes only the connector's public order and
    cancellation methods.  It does not know credentials, construct a request,
    sign payloads, or call a Derive endpoint.  The current command never calls
    these methods because Iteration 0 is dry-run only.
    """

    def __init__(
        self,
        connector: Any,
        *,
        runtime_probe: Mapping[str, Any] | None = None,
        executor_provider: Any | None = None,
    ):
        self.connector = connector
        self.runtime_probe = dict(runtime_probe or {})
        self.executor_provider = executor_provider

    def submit_open(self, trading_pair: str, plan: Any) -> str:
        from hummingbot.core.data_type.common import OrderType, PositionAction

        method = self.connector.buy if plan.side == "BUY" else self.connector.sell
        return method(
            trading_pair=trading_pair,
            amount=plan.amount_base,
            order_type=OrderType.LIMIT_MAKER,
            price=plan.price,
            position_action=PositionAction.OPEN,
        )

    def submit_close(self, trading_pair: str, plan: Any) -> str:
        from hummingbot.core.data_type.common import OrderType, PositionAction

        method = self.connector.buy if plan.side == "BUY" else self.connector.sell
        return method(
            trading_pair=trading_pair,
            amount=plan.amount_base,
            order_type=OrderType.LIMIT_MAKER,
            price=plan.price,
            position_action=PositionAction.CLOSE,
        )

    def cancel(self, trading_pair: str, client_order_id: str) -> Any:
        return self.connector.cancel(trading_pair, client_order_id)

    def wait_for_order(
        self, trading_pair: str, client_order_id: str, timeout_seconds: float
    ) -> Mapping[str, Any]:
        """Read the normal connector order tracker for a bounded interval.

        The adapter intentionally does not poll an exchange endpoint itself.
        Hummingbot's connector/user stream owns order updates; the canary only
        observes that in-memory state and stops on an unknown outcome.
        """

        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        last: dict[str, Any] = {}
        terminal_states = {"FILLED", "CANCELED", "CANCELLED", "FAILED", "EXPIRED"}
        while time.monotonic() <= deadline:
            order = None
            orders = getattr(self.connector, "in_flight_orders", None)
            if isinstance(orders, Mapping):
                order = orders.get(client_order_id)
            if order is not None:
                state = str(getattr(order, "current_state", "")).upper()
                filled = getattr(order, "executed_amount_base", None)
                last = {
                    "status": state,
                    "fill_count": 1 if filled not in (None, Decimal("0"), 0) else 0,
                    "filled_amount_base": filled,
                    "resting_verified": state in {"OPEN", "PENDING_CREATE", "CREATED"},
                    "active_orders": 1,
                }
                if state in terminal_states:
                    break
            # This short sleep is deliberately bounded and does not retry a
            # request; the connector's own background tasks provide updates.
            time.sleep(0.05)
        if not last:
            return {"status": "UNKNOWN", "resting_verified": False}
        return last

    def read_account(self, trading_pair: str) -> dict[str, Any]:
        position_base = Decimal("0")
        positions = getattr(self.connector, "account_positions", None)
        if isinstance(positions, Mapping):
            for pair, position in positions.items():
                if getattr(position, "trading_pair", pair) != trading_pair:
                    continue
                amount = Decimal(str(getattr(position, "amount", "0")))
                side = str(getattr(getattr(position, "position_side", ""), "value", ""))
                position_base += -abs(amount) if side.upper().endswith("SHORT") else abs(amount)
        orders = getattr(self.connector, "in_flight_orders", None)
        active_orders = None
        if isinstance(orders, Mapping):
            active_orders = sum(
                1
                for order in orders.values()
                if getattr(order, "trading_pair", trading_pair) == trading_pair
            )
        available = None
        try:
            available = self.connector.get_available_balance("USDC")
        except Exception:
            available = None
        account_binding = _account_binding(self.connector)
        account = {
            "account_fingerprint": account_binding.get("account_fingerprint"),
            "account_fingerprint_scheme": account_binding.get("fingerprint_scheme"),
            "position_base": position_base,
            "active_orders": active_orders,
            "managed_executors": None,
            "unmanaged_executors": None,
            "available_collateral_quote": available,
        }
        if callable(self.executor_provider):
            executor_state = self.executor_provider()
            if isinstance(executor_state, Mapping):
                account.update(
                    {
                        "managed_executors": executor_state.get("managed_executors"),
                        "unmanaged_executors": executor_state.get("unmanaged_executors"),
                    }
                )
        return self._bind_section(account)

    def read_bbo(self, trading_pair: str) -> dict[str, Any]:
        book = self.connector.get_order_book(trading_pair)
        bid_row = next(iter(book.bid_entries()), None)
        ask_row = next(iter(book.ask_entries()), None)
        return self._bind_section(
            {
                "best_bid": getattr(bid_row, "price", None),
                "best_ask": getattr(ask_row, "price", None),
                "observed_at": time.time(),
                "snapshot_uid": getattr(book, "snapshot_uid", None),
                "last_update_id": getattr(book, "last_update_id", None),
            }
        )

    def read_rules(self, trading_pair: str) -> dict[str, Any]:
        rules = getattr(self.connector, "trading_rules", {})
        rule = rules.get(trading_pair) if isinstance(rules, Mapping) else None
        if rule is None:
            return self._bind_section({})
        return self._bind_section(
            {
                "min_base_amount": getattr(rule, "min_order_size", None),
                "amount_increment": getattr(rule, "min_base_amount_increment", None),
                "min_notional": getattr(rule, "min_notional_size", None),
                "price_increment": getattr(rule, "min_price_increment", None),
            }
        )

    def _bind_section(self, section: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **dict(section),
            "runtime_instance_id": self.runtime_probe.get("runtime_instance_id"),
            "runtime_contract_id": self.runtime_probe.get("runtime_contract_id"),
            "runtime_image_ref": self.runtime_probe.get("runtime_image_ref"),
        }

    def snapshot(self, trading_pair: str) -> dict[str, Any]:
        account = self.read_account(trading_pair)
        bbo = self.read_bbo(trading_pair)
        rules = self.read_rules(trading_pair)
        static_contract = self._bind_section(
            self.runtime_probe.get(
                "static_contract",
                {
                    "open_reduce_only": False,
                    "close_reduce_only": True,
                    "limit_tif": "gtc",
                    "limit_maker_tif": "post_only",
                    "maker_fee_decimal": "0.0001",
                    "taker_fee_decimal": "0.0003",
                },
            )
        )
        return {
            "identity": {
                "environment": "mainnet",
                "connector_name": "derive_perpetual",
                "trading_pair": trading_pair,
                "exchange_instrument": "SOL-PERP",
                "position_mode": "ONEWAY",
                "leverage": 1,
            },
            "runtime": dict(self.runtime_probe),
            "expected_runtime": dict(self.runtime_probe),
            "static_contract": static_contract,
            "account": account,
            "bbo": bbo,
            "rules": rules,
        }


def _account_binding(connector: Any) -> dict[str, Any]:
    try:
        from derive_options_adaptive_grid.connector_contract import account_binding_from_connector

        return dict(account_binding_from_connector(connector) or {})
    except Exception:
        return {}


def _load_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "identity": {
                "environment": "mainnet",
                "connector_name": "derive_perpetual",
                "trading_pair": "SOL-USDC",
                "exchange_instrument": "SOL-PERP",
                "position_mode": "ONEWAY",
                "leverage": 1,
            },
            "runtime": {},
            "expected_runtime": {},
            "static_contract": {},
            "account": {},
            "bbo": {},
            "rules": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("snapshot JSON must contain an object")
    return payload


def _runtime_probe(connector: Any | None = None) -> dict[str, Any]:
    try:
        from derive_options_adaptive_grid.connector_contract import (
            inspect_connector_contract,
            runtime_instance_binding,
        )

        snapshot = inspect_connector_contract(connector)
        binding = runtime_instance_binding()
    except Exception as exc:  # pragma: no cover - depends on installed Hummingbot
        return {"probe_error": type(exc).__name__}
    return {
        "base_image_digest": snapshot.base_image_digest,
        "runtime_contract_id": snapshot.runtime_contract_id,
        "runtime_instance_id": binding.get("runtime_instance_id"),
        "runtime_image_ref": binding.get("runtime_image_ref"),
        "account_fingerprint": snapshot.runtime_account_fingerprint,
        "account_fingerprint_scheme": snapshot.runtime_account_fingerprint_scheme,
        "connector_source_sha256": snapshot.source_hash,
        "fee_source_sha256": snapshot.fee_source_hash,
        "runtime_manifest_verified": snapshot.runtime_manifest_verified,
        "static_contract": {
            "open_reduce_only": snapshot.open_reduce_only,
            "close_reduce_only": snapshot.close_contract_verified,
            "limit_tif": snapshot.limit_tif,
            "limit_maker_tif": snapshot.limit_maker_tif,
            "maker_fee_decimal": "0.0001",
            "taker_fee_decimal": "0.0003",
        },
        "lifecycle_verified": snapshot.lifecycle_verified,
        "lifecycle_blockers": list(snapshot.lifecycle_evidence_blockers),
    }


def build_runtime_snapshot(
    connector: Any,
    *,
    trading_pair: str = "SOL-USDC",
    executor_provider: Any | None = None,
) -> dict[str, Any]:
    """Capture all preflight sections from one live Hummingbot connector.

    This is an integration hook for a Hummingbot script/controller process;
    it is intentionally not called by the ordinary CLI.  It produces the
    runtime, account, BBO, trading-rule, and static-contract sections with the
    same container identity and contract ID, so a file assembled from mixed
    bots is rejected by ``build_dry_run_result``.
    """

    _add_repo_paths()
    probe = _runtime_probe(connector)
    adapter = HummingbotNativeLifecycleAdapter(
        connector,
        runtime_probe=probe,
        executor_provider=executor_provider,
    )
    return adapter.snapshot(trading_pair)


def build_report(snapshot: dict[str, Any], *, side: str = "BUY") -> dict[str, Any]:
    _add_repo_paths()
    from derive_options_adaptive_grid.lifecycle_canary import build_dry_run_result

    runtime_probe = _runtime_probe()
    merged = dict(snapshot)
    if not merged.get("runtime"):
        merged["runtime"] = {
            key: value
            for key, value in runtime_probe.items()
            if key in {
                "base_image_digest",
                "runtime_contract_id",
                "connector_source_sha256",
                "fee_source_sha256",
                "runtime_instance_id",
                "runtime_image_ref",
            }
        }
    if not merged.get("expected_runtime"):
        merged["expected_runtime"] = {
            key: runtime_probe.get(key)
            for key in (
                "base_image_digest",
                "runtime_contract_id",
                "connector_source_sha256",
                "fee_source_sha256",
                "runtime_instance_id",
                "runtime_image_ref",
            )
        }
    if not merged.get("static_contract"):
        merged["static_contract"] = dict(runtime_probe.get("static_contract", {}))
        merged["static_contract"].update(
            {
                "maker_fee_decimal": "0.0001",
                "taker_fee_decimal": "0.0003",
                "runtime_instance_id": runtime_probe.get("runtime_instance_id"),
                "runtime_contract_id": runtime_probe.get("runtime_contract_id"),
                "runtime_image_ref": runtime_probe.get("runtime_image_ref"),
            }
        )
    result = build_dry_run_result(merged, side=side)
    report = result.as_dict()
    report.update(
        {
            "identity": merged.get("identity", {}),
            "runtime_probe": runtime_probe,
            "execution_enabled": False,
            "mainnet_armed": False,
            "orders_submitted": 0,
            "orders_cancelled": 0,
            "fills": 0,
            "position_changes": 0,
            "credentials_changed": False,
            "mutation_attempted": False,
            "runtime_instance_id": runtime_probe.get("runtime_instance_id"),
            "runtime_image_ref": runtime_probe.get("runtime_image_ref"),
            "stop_code": "READY_FOR_EXPLICIT_MAINNET_LIFECYCLE_AUTHORIZATION"
            if not result.preflight_blockers
            else "READ_ONLY_PREFLIGHT_BLOCKED",
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("DRY_RUN", "STAGE_A", "STAGE_B"), default="DRY_RUN")
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="reserved for a future separately reviewed Hummingbot-native stage",
    )
    args = parser.parse_args()
    if args.mode != "DRY_RUN":
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "authorized": False,
                    "mutation_attempted": False,
                    "blockers": [
                        "MUTATION_DISABLED_IN_ITERATION_0",
                        "USE_NORMAL_HUMMINGBOT_EXECUTOR_API_ONLY",
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    report = build_report(_load_snapshot(args.snapshot), side=args.side)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["stop_code"] == "READY_FOR_EXPLICIT_MAINNET_LIFECYCLE_AUTHORIZATION" else 1


if __name__ == "__main__":
    raise SystemExit(main())
