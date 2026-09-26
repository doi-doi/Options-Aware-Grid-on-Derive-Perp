# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""Fail-closed proof of the installed Derive connector wire contract.

This module deliberately inspects the connector that is loaded by the
running Hummingbot Python.  It does not send an exchange request: payloads
are built with the connector's normal request builder while transport is
replaced with a local capture function.

The expected source hashes are filled from the reproducible derived bot image
in ``runtime/derive-options-grid-hummingbot``.  A different connector build
must fail closed until its source is reviewed and the image contract is
updated deliberately.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .lifecycle_evidence import (
    ACCOUNT_FINGERPRINT_SCHEME,
    EXPECTED_STATIC_CONTRACT,
    PROMOTED_LIFECYCLE_EVIDENCE_PATH,
    compute_account_fingerprint,
    compute_runtime_contract_id,
    validate_promoted_lifecycle_evidence,
)

# These are intentionally not configurable.  They are populated after the
# derived image is built from the pinned Hummingbot image and its source hash
# is independently verified.
EXPECTED_DERIVE_ORDER_SOURCE_SHA256 = (
    "30f0b56079437a14b6497e3a23de92a640bf35120478db7a7bbcb678975c5fe5"
)
EXPECTED_DERIVE_UTILS_SOURCE_SHA256 = (
    "f93d1cf3f0c80b21fe11a3dccb41beda808a3e511ebf56c6cd385ed513e87637"
)
RUNTIME_CONTRACT_MANIFEST_PATH = Path(
    "/opt/derive-options-grid-runtime/runtime_contract_manifest.json"
)
RUNTIME_INSTANCE_ENV = "DERIVE_OPTIONS_GRID_RUNTIME_INSTANCE_ID"
RUNTIME_IMAGE_ENV = "DERIVE_OPTIONS_GRID_RUNTIME_IMAGE_REF"
_OVERLAY_UPPERDIR_PATTERN = re.compile(r"(?:^|,)upperdir=([^,\s]+)")
_FALLBACK_FRESHNESS_TRACKERS: dict[tuple[int, str], OrderBookFreshnessTracker] = {}


@dataclass(frozen=True)
class ConnectorContractSnapshot:
    """Immutable runtime evidence consumed by controller readiness gates."""

    module_path: str | None = None
    runtime_version: str | None = None
    source_hash: str | None = None
    source_hash_verified: bool = False
    fee_source_hash: str | None = None
    fee_source_hash_verified: bool = False
    open_reduce_only: bool = False
    close_reduce_only: bool = False
    limit_tif: str | None = None
    limit_maker_tif: str | None = None
    open_contract_verified: bool = False
    close_contract_verified: bool = False
    post_only_verified: bool = False
    contract_verified: bool = False
    lifecycle_verified: bool = False
    runtime_manifest_path: str | None = None
    runtime_manifest_sha256: str | None = None
    runtime_manifest_verified: bool = False
    base_image_digest: str | None = None
    runtime_contract_id: str | None = None
    runtime_instance_id: str | None = None
    runtime_image_ref: str | None = None
    runtime_account_fingerprint: str | None = None
    runtime_account_fingerprint_scheme: str | None = None
    lifecycle_evidence_path: str | None = str(PROMOTED_LIFECYCLE_EVIDENCE_PATH)
    lifecycle_evidence_present: bool = False
    lifecycle_evidence_id: str | None = None
    lifecycle_evidence_sha256: str | None = None
    lifecycle_evidence_approved: bool = False
    lifecycle_evidence_generated_at: str | None = None
    lifecycle_evidence_runtime_instance_id: str | None = None
    lifecycle_stage_a_status: str | None = None
    lifecycle_stage_b_status: str | None = None
    lifecycle_cleanup_status: str | None = None
    lifecycle_runtime_identity_match: bool = False
    lifecycle_account_identity_match: bool = False
    lifecycle_evidence_blockers: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderBookFreshnessTracker:
    """Track freshness from Hummingbot's order-book update identity.

    ``get_order_book`` returns a cached object.  Reading that object again is
    not a new market-data observation, so wall-clock time must only advance
    when the connector exposes a new ``(snapshot_uid, last_update_id)``
    marker.  This mirrors the controller's observation clock and prevents a
    stale cache from looking fresh merely because it was read again.
    """

    marker: tuple[Any, Any] | None = None
    observed_at: float | None = None

    def observe(
        self,
        marker: tuple[Any, Any] | None,
        *,
        now: float | None = None,
    ) -> tuple[float | None, bool]:
        if marker is None or (marker[0] is None and marker[1] is None):
            return self.observed_at, False
        current_time = time.time() if now is None else float(now)
        changed = marker != self.marker
        if changed:
            self.marker = marker
            self.observed_at = current_time
        return self.observed_at, changed


def _sha256(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None


def _runtime_version() -> str | None:
    for distribution in ("hummingbot", "hummingbot-api"):
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    try:
        hummingbot = importlib.import_module("hummingbot")
    except Exception:
        return None
    return getattr(hummingbot, "__version__", None)


def _runtime_instance_id_from_mountinfo(mountinfo: str) -> str | None:
    """Derive a stable, non-secret per-container ID from Docker overlay metadata.

    Docker Desktop gives every bot the same hostname (``docker-desktop``), so
    hostname alone cannot bind account, BBO, rules, and contract reads to one
    running instance.  The overlay upper directory is unique to the container;
    only its one-way digest is retained or reported.
    """

    for line in mountinfo.splitlines():
        if " - overlay " not in line:
            continue
        match = _OVERLAY_UPPERDIR_PATTERN.search(line)
        if match is None:
            continue
        digest = hashlib.sha256(
            f"derive-options-grid-runtime-instance-v1:{match.group(1)}".encode()
        ).hexdigest()
        return f"sha256:{digest}"
    return None


def _detected_runtime_instance_id() -> str | None:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    return _runtime_instance_id_from_mountinfo(mountinfo)


def runtime_instance_binding() -> dict[str, str | None]:
    """Return the non-secret identity shared by all code in one bot container."""

    instance_id = (
        os.environ.get(RUNTIME_INSTANCE_ENV)
        or _detected_runtime_instance_id()
        or socket.gethostname()
    )
    image_ref = os.environ.get(RUNTIME_IMAGE_ENV)
    return {
        "runtime_instance_id": instance_id.strip() if instance_id else None,
        "runtime_image_ref": image_ref.strip() if image_ref else None,
    }


def account_binding_from_connector(connector: Any) -> dict[str, str] | None:
    """Derive a one-way account binding from public connector identity fields.

    The Derive connector stores the public owner address in
    ``derive_perpetual_api_key`` and the subaccount in ``_sub_id``.  This
    helper never reads the secret field and never returns the owner address.
    """

    try:
        public_owner = getattr(connector, "derive_perpetual_api_key", None)
        subaccount_id = getattr(connector, "_sub_id", None)
        account_type = getattr(connector, "_account_type", None)
        domain = getattr(connector, "domain", None)
        if not isinstance(public_owner, str) or not public_owner.strip():
            return None
        if subaccount_id is None or account_type is None or domain is None:
            return None
        connector_name = getattr(connector, "name", None) or "derive_perpetual"
        fingerprint = compute_account_fingerprint(
            connector_name=str(connector_name),
            environment=("testnet" if "testnet" in str(domain).lower() else "mainnet"),
            domain=str(domain),
            account_type=str(account_type),
            subaccount_id=subaccount_id,
            public_owner=public_owner,
        )
    except Exception:
        return None
    if not fingerprint:
        return None
    return {
        "account_fingerprint": fingerprint,
        "fingerprint_scheme": ACCOUNT_FINGERPRINT_SCHEME,
    }


def _default_order_book_freshness_tracker(
    connector: Any, trading_pair: str
) -> OrderBookFreshnessTracker:
    """Keep a tracker for callers that do not need to own its lifecycle."""

    attribute_name = "_derive_options_grid_order_book_freshness"
    trackers = getattr(connector, attribute_name, None)
    if not isinstance(trackers, dict):
        trackers = {}
        try:
            setattr(connector, attribute_name, trackers)
        except Exception:
            # A connector can be implemented with slots. Keep the same
            # marker clock outside the object in that case.
            key = (id(connector), trading_pair)
            return _FALLBACK_FRESHNESS_TRACKERS.setdefault(
                key, OrderBookFreshnessTracker()
            )
    tracker = trackers.get(trading_pair)
    if not isinstance(tracker, OrderBookFreshnessTracker):
        tracker = OrderBookFreshnessTracker()
        trackers[trading_pair] = tracker
    return tracker


def build_hummingbot_runtime_snapshot(
    connector: Any,
    *,
    trading_pair: str = "SOL-USDC",
    freshness_tracker: OrderBookFreshnessTracker | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Capture a read-only, single-container canary snapshot.

    The snapshot has the same shape used by the pure lifecycle checks.  It
    reads only the live connector's cached account, order book, and trading
    rules.  It does not call an endpoint, sign a request, mutate account
    settings, or expose the public account owner.
    """

    contract = inspect_connector_contract(connector)
    binding = runtime_instance_binding()
    runtime = {
        "base_image_digest": contract.base_image_digest,
        "runtime_contract_id": contract.runtime_contract_id,
        "runtime_instance_id": binding.get("runtime_instance_id"),
        "runtime_image_ref": binding.get("runtime_image_ref"),
        "connector_source_sha256": contract.source_hash,
        "fee_source_sha256": contract.fee_source_hash,
    }

    def bound(section: Mapping[str, Any]) -> dict[str, Any]:
        return {**dict(section), **{key: runtime.get(key) for key in (
            "runtime_instance_id", "runtime_contract_id", "runtime_image_ref"
        )}}

    position_base = Decimal("0")
    positions = getattr(connector, "account_positions", {})
    if isinstance(positions, Mapping):
        for key, position in positions.items():
            if getattr(position, "trading_pair", key) != trading_pair:
                continue
            amount = Decimal(str(getattr(position, "amount", "0")))
            side = str(getattr(getattr(position, "position_side", None), "value", ""))
            position_base += -abs(amount) if side.upper().endswith("SHORT") else abs(amount)

    orders = getattr(connector, "in_flight_orders", {})
    active_orders = None
    if isinstance(orders, Mapping):
        active_orders = sum(
            1
            for order in orders.values()
            if getattr(order, "trading_pair", trading_pair) == trading_pair
        )
    try:
        available_collateral = connector.get_available_balance("USDC")
    except Exception:
        available_collateral = None
    account = bound(
        {
            **(account_binding_from_connector(connector) or {}),
            "position_base": position_base,
            "active_orders": active_orders,
            "managed_executors": 0,
            "unmanaged_executors": 0,
            "available_collateral_quote": available_collateral,
        }
    )

    book = connector.get_order_book(trading_pair)
    bid_row = next(iter(book.bid_entries()), None)
    ask_row = next(iter(book.ask_entries()), None)
    snapshot_uid = getattr(book, "snapshot_uid", None)
    last_update_id = getattr(book, "last_update_id", None)
    order_book_marker = (
        (snapshot_uid, last_update_id)
        if snapshot_uid is not None or last_update_id is not None
        else None
    )
    tracker = freshness_tracker or _default_order_book_freshness_tracker(
        connector, trading_pair
    )
    observed_at, marker_changed = tracker.observe(order_book_marker, now=now)
    bbo = bound(
        {
            "best_bid": getattr(bid_row, "price", None),
            "best_ask": getattr(ask_row, "price", None),
            "observed_at": observed_at,
            "snapshot_uid": snapshot_uid,
            "last_update_id": last_update_id,
            "order_book_marker": order_book_marker,
            "marker_changed": marker_changed,
        }
    )
    rules = getattr(connector, "trading_rules", {})
    rule = rules.get(trading_pair) if isinstance(rules, Mapping) else None
    rule_snapshot = bound(
        {
            "min_base_amount": getattr(rule, "min_order_size", None),
            "amount_increment": getattr(rule, "min_base_amount_increment", None),
            "min_notional": getattr(rule, "min_notional_size", None),
            "price_increment": getattr(rule, "min_price_increment", None),
        }
    )
    static_contract = bound(
        {
            "open_reduce_only": contract.open_reduce_only,
            "close_reduce_only": contract.close_contract_verified,
            "limit_tif": contract.limit_tif,
            "limit_maker_tif": contract.limit_maker_tif,
            "maker_fee_decimal": "0.0001",
            "taker_fee_decimal": "0.0003",
        }
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
        "runtime": runtime,
        "expected_runtime": dict(runtime),
        "static_contract": static_contract,
        "account": account,
        "bbo": bbo,
        "rules": rule_snapshot,
    }


def _read_runtime_manifest() -> tuple[dict[str, Any], Path | None]:
    """Read the immutable image manifest without falling back to a config."""

    candidates = (
        RUNTIME_CONTRACT_MANIFEST_PATH,
        Path(__file__).resolve().parents[2]
        / "runtime/derive-options-grid-hummingbot/runtime_contract_manifest.json",
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload, path
    return {}, None


async def _capture_payload(
    order_type: Any, position_action: Any, trade_type: Any
) -> dict[str, Any]:
    connector_module = importlib.import_module(
        "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_derivative"
    )
    connector_class = connector_module.DerivePerpetualDerivative
    connector = connector_class.__new__(connector_class)
    connector._sub_id = "0"
    connector._instrument_ticker = [
        {
            "instrument_name": "SOL-PERP",
            "base_asset_address": "0x0",
            "base_asset_sub_id": "0",
        }
    ]
    captured: dict[str, Any] = {}

    async def associated_symbol(*, trading_pair: str) -> str:
        return "SOL-PERP"

    async def trading_pairs_request() -> list[dict[str, str]]:
        return connector._instrument_ticker

    async def api_post(*, path_url: str, data: dict[str, Any], is_auth_required: bool):
        captured.update(data)
        return {"result": {"order": {"order_id": "probe", "creation_timestamp": 0}}}

    connector.exchange_symbol_associated_to_pair = associated_symbol
    connector._make_trading_pairs_request = trading_pairs_request
    connector._api_post = api_post
    await connector_class._place_order(
        connector,
        order_id="probe",
        trading_pair="SOL-USDC",
        amount=Decimal("0.1"),
        trade_type=trade_type,
        order_type=order_type,
        price=Decimal("100"),
        position_action=position_action,
    )
    return captured


async def _capture_payload_matrix(common: Any) -> dict[str, dict[str, Any]]:
    """Build all normal connector payloads on one local event loop."""

    return {
        "limit_open_buy": await _capture_payload(
            common.OrderType.LIMIT, common.PositionAction.OPEN, common.TradeType.BUY
        ),
        "limit_maker_open_buy": await _capture_payload(
            common.OrderType.LIMIT_MAKER,
            common.PositionAction.OPEN,
            common.TradeType.BUY,
        ),
        "limit_maker_close_buy": await _capture_payload(
            common.OrderType.LIMIT_MAKER,
            common.PositionAction.CLOSE,
            common.TradeType.BUY,
        ),
        "limit_maker_close_sell": await _capture_payload(
            common.OrderType.LIMIT_MAKER,
            common.PositionAction.CLOSE,
            common.TradeType.SELL,
        ),
        "market_close_sell": await _capture_payload(
            common.OrderType.MARKET,
            common.PositionAction.CLOSE,
            common.TradeType.SELL,
        ),
    }


def _payload_matrix() -> dict[str, dict[str, Any]]:
    """Build the normal connector payloads without network or signing."""

    common = importlib.import_module("hummingbot.core.data_type.common")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_capture_payload_matrix(common))

    result: list[dict[str, dict[str, Any]]] = []
    errors: list[BaseException] = []

    def run_in_probe_thread() -> None:
        try:
            result.append(asyncio.run(_capture_payload_matrix(common)))
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            errors.append(exc)

    thread = threading.Thread(
        target=run_in_probe_thread,
        name="derive-connector-contract-probe",
        daemon=True,
    )
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def inspect_connector_contract(
    connector: Any | None = None,
    *,
    expected_account: Mapping[str, Any] | None = None,
) -> ConnectorContractSnapshot:
    """Return static proof plus lifecycle evidence bound to one connector.

    Passing a live connector is optional for the static probe, but required
    for account-bound lifecycle validation.  The static payload matrix remains
    independent and never uses the live connector for transport.
    """

    errors: list[str] = []
    try:
        connector_module = importlib.import_module(
            "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_derivative"
        )
        utils_module = importlib.import_module(
            "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_utils"
        )
        module_path = str(Path(connector_module.__file__).resolve())
        source_hash = _sha256(module_path)
        fee_source_hash = _sha256(utils_module.__file__)
        source_hash_verified = bool(
            EXPECTED_DERIVE_ORDER_SOURCE_SHA256
            and source_hash == EXPECTED_DERIVE_ORDER_SOURCE_SHA256
        )
        fee_source_hash_verified = bool(
            EXPECTED_DERIVE_UTILS_SOURCE_SHA256
            and fee_source_hash == EXPECTED_DERIVE_UTILS_SOURCE_SHA256
        )
        if not source_hash_verified:
            errors.append("connector_source_hash_unverified")
        if not fee_source_hash_verified:
            errors.append("fee_source_hash_unverified")

        source = inspect.getsource(connector_module.DerivePerpetualDerivative._place_order)
        if 'param_order_type = "post_only"' not in source:
            errors.append("limit_maker_post_only_source_missing")
        if '"reduce_only": position_action == PositionAction.CLOSE' not in source:
            errors.append("position_action_reduce_only_source_missing")

        matrix = _payload_matrix()
        limit_open = matrix["limit_open_buy"]
        maker_open = matrix["limit_maker_open_buy"]
        maker_close = matrix["limit_maker_close_buy"]
        maker_close_sell = matrix["limit_maker_close_sell"]
        market_close = matrix["market_close_sell"]
        close_reduce_only = maker_close.get("reduce_only") is True
        open_contract_verified = (
            limit_open.get("reduce_only") is False
            and maker_open.get("reduce_only") is False
        )
        close_contract_verified = (
            maker_close.get("reduce_only") is True
            and maker_close_sell.get("reduce_only") is True
            and market_close.get("reduce_only") is True
        )
        post_only_verified = (
            limit_open.get("time_in_force") == "gtc"
            and maker_open.get("time_in_force") == "post_only"
            and maker_close.get("time_in_force") == "post_only"
        )
        contract_verified = bool(
            source_hash_verified
            and fee_source_hash_verified
            and open_contract_verified
            and close_contract_verified
            and post_only_verified
        )
        if not open_contract_verified:
            errors.append("open_reduce_only_contract_failed")
        if not close_contract_verified:
            errors.append("close_reduce_only_contract_failed")
        if not post_only_verified:
            errors.append("limit_maker_post_only_contract_failed")
        manifest, manifest_path = _read_runtime_manifest()
        base_image_digest = manifest.get("base_image_digest")
        runtime_contract_id = compute_runtime_contract_id(
            base_image_digest, source_hash, fee_source_hash
        )
        manifest_contract = manifest.get("contract")
        manifest_verified = bool(
            manifest
            and manifest.get("patched_order_source_sha256") == source_hash
            and manifest.get("patched_utils_source_sha256") == fee_source_hash
            and manifest.get("fee_unit") == "decimal_fraction"
            and manifest.get("mainnet_maker_fee_decimal")
            == EXPECTED_STATIC_CONTRACT["maker_fee_decimal"]
            and manifest.get("mainnet_taker_fee_decimal")
            == EXPECTED_STATIC_CONTRACT["taker_fee_decimal"]
            and isinstance(manifest_contract, dict)
            and manifest_contract.get("open_reduce_only") is False
            and manifest_contract.get("close_reduce_only") is True
            and manifest_contract.get("limit_time_in_force")
            == EXPECTED_STATIC_CONTRACT["limit_tif"]
            and manifest_contract.get("limit_maker_time_in_force")
            == EXPECTED_STATIC_CONTRACT["limit_maker_tif"]
        )
        if not manifest_verified:
            errors.append("runtime_contract_manifest_unverified")
        runtime_binding = runtime_instance_binding()
        account_binding = (
            account_binding_from_connector(connector) if connector is not None else None
        )
        lifecycle_expected_runtime = {
            "base_image_digest": base_image_digest,
            "runtime_contract_id": runtime_contract_id,
            "connector_source_sha256": source_hash,
            "fee_source_sha256": fee_source_hash,
            # The artifact retains the canary instance ID for audit, but
            # promoted validation must remain reusable by a fresh controller
            # instance built from this same reviewed runtime.
            "runtime_image_ref": runtime_binding.get("runtime_image_ref"),
        }
        lifecycle_expected_account = expected_account
        if lifecycle_expected_account is None and account_binding is not None:
            lifecycle_expected_account = {
                "account_fingerprint": account_binding["account_fingerprint"],
                "clean_at_start": True,
            }
        lifecycle_validation = validate_promoted_lifecycle_evidence(
            expected_runtime=lifecycle_expected_runtime,
            expected_account=lifecycle_expected_account,
            expected_static_contract={
                "open_reduce_only": limit_open.get("reduce_only"),
                "close_reduce_only": maker_close.get("reduce_only"),
                "limit_tif": limit_open.get("time_in_force"),
                "limit_maker_tif": maker_open.get("time_in_force"),
                "maker_fee_decimal": manifest.get(
                    "mainnet_maker_fee_decimal", EXPECTED_STATIC_CONTRACT["maker_fee_decimal"]
                ),
                "taker_fee_decimal": manifest.get(
                    "mainnet_taker_fee_decimal", EXPECTED_STATIC_CONTRACT["taker_fee_decimal"]
                ),
            },
        )
        lifecycle_errors = list(lifecycle_validation.blockers)
        return ConnectorContractSnapshot(
            module_path=module_path,
            runtime_version=_runtime_version(),
            source_hash=source_hash,
            source_hash_verified=source_hash_verified,
            fee_source_hash=fee_source_hash,
            fee_source_hash_verified=fee_source_hash_verified,
            open_reduce_only=limit_open.get("reduce_only") is True,
            close_reduce_only=close_reduce_only,
            limit_tif=limit_open.get("time_in_force"),
            limit_maker_tif=maker_open.get("time_in_force"),
            open_contract_verified=open_contract_verified,
            close_contract_verified=close_contract_verified,
            post_only_verified=post_only_verified,
            contract_verified=contract_verified,
            lifecycle_verified=bool(contract_verified and lifecycle_validation.valid),
            runtime_manifest_path=str(manifest_path) if manifest_path else None,
            runtime_manifest_sha256=_sha256(manifest_path) if manifest_path else None,
            runtime_manifest_verified=manifest_verified,
            base_image_digest=base_image_digest,
            runtime_contract_id=runtime_contract_id,
            runtime_instance_id=runtime_binding.get("runtime_instance_id"),
            runtime_image_ref=runtime_binding.get("runtime_image_ref"),
            runtime_account_fingerprint=(
                account_binding.get("account_fingerprint") if account_binding else None
            ),
            runtime_account_fingerprint_scheme=(
                account_binding.get("fingerprint_scheme") if account_binding else None
            ),
            lifecycle_evidence_path=str(PROMOTED_LIFECYCLE_EVIDENCE_PATH),
            lifecycle_evidence_present=lifecycle_validation.artifact_present,
            lifecycle_evidence_id=lifecycle_validation.evidence_id,
            lifecycle_evidence_sha256=lifecycle_validation.evidence_sha256,
            lifecycle_evidence_approved=lifecycle_validation.valid,
            lifecycle_evidence_generated_at=lifecycle_validation.generated_at,
            lifecycle_evidence_runtime_instance_id=(
                lifecycle_validation.evidence_runtime_instance_id
            ),
            lifecycle_stage_a_status=lifecycle_validation.stage_a_status,
            lifecycle_stage_b_status=lifecycle_validation.stage_b_status,
            lifecycle_cleanup_status=lifecycle_validation.cleanup_status,
            lifecycle_runtime_identity_match=lifecycle_validation.runtime_identity_match,
            lifecycle_account_identity_match=lifecycle_validation.account_identity_match,
            lifecycle_evidence_blockers=tuple(lifecycle_errors),
            errors=tuple(dict.fromkeys([*errors, *lifecycle_errors])),
        )
    except Exception as exc:
        errors.append(f"connector_contract_probe_error:{type(exc).__name__}")
        return ConnectorContractSnapshot(
            runtime_version=_runtime_version(),
            runtime_manifest_path=str(RUNTIME_CONTRACT_MANIFEST_PATH),
            lifecycle_evidence_path=str(PROMOTED_LIFECYCLE_EVIDENCE_PATH),
            lifecycle_evidence_blockers=("connector_contract_probe_failed",),
            errors=tuple(dict.fromkeys(errors)),
        )


def normalized_fee_observation(value: Any) -> dict[str, Any]:
    """Describe a TradeFeeSchema value without guessing or double-converting."""

    try:
        raw = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return {
            "raw_value": None,
            "raw_unit": "decimal_fraction",
            "normalized_decimal_rate": None,
        }
    return {
        "raw_value": raw,
        "raw_unit": "decimal_fraction",
        "normalized_decimal_rate": raw,
    }
