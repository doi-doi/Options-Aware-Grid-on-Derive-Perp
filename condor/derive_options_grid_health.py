# DERIVE_OPTIONS_ADAPTIVE_GRID_CONDOR_MANAGED
"""Read-only operator dashboard for the Derive SOL adaptive grid.

The routine reads Hummingbot status only. It never arms mainnet, changes a
controller, creates/stops an executor, cancels an order, or calls Derive
directly. The normalized snapshot is the stable contract consumed by the
evidence routine and by tests.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, model_validator

CATEGORY = "Monitoring"
CONTINUOUS = True
ASSETS = ("SOL",)
HEALTH_STATUSES = frozenset({"HEALTHY", "DEGRADED_DATA", "EXECUTION_BLOCKED", "CRITICAL"})
EXECUTION_STATES = frozenset(
    {"SHADOW_RUNNING", "LIVE_DEARMED", "LIVE_ENABLED", "CASHED_OUT", "UNKNOWN"}
)
GRID_MODES = frozenset({"AGGRESSIVE", "NORMAL", "DEFENSIVE"})


class Config(BaseModel):
    bot_name: str = Field(default="derive-options-adaptive-grid-sol-three-mode-shadow")
    poll_interval_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    report_auto_refresh_seconds: int = Field(default=3, ge=1, le=60)
    execution_enabled: bool = False

    @model_validator(mode="after")
    def read_only(self):
        if self.execution_enabled:
            raise ValueError("derive_options_grid_health is read-only")
        return self


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _find_sol(data: Any) -> dict[str, Any]:
    if isinstance(data, Mapping):
        if str(data.get("asset", "")).upper() == "SOL":
            return dict(data)
        for value in data.values():
            found = _find_sol(value)
            if found:
                return found
    elif isinstance(data, (list, tuple)):
        for value in data:
            found = _find_sol(value)
            if found:
                return found
    return {}


def _unwrap(payload: Any) -> Mapping[str, Any]:
    root = _mapping(payload)
    if str(root.get("status", "")).lower() == "success" and isinstance(root.get("data"), Mapping):
        return _mapping(root["data"])
    return root


def _execution_state(controller: Mapping[str, Any]) -> str:
    if not controller:
        return "UNKNOWN"
    execution = _mapping(controller.get("execution"))
    runtime = _mapping(controller.get("runtime"))
    armed = bool(execution.get("mainnet_armed", False))
    enabled = bool(execution.get("execution_enabled", False))
    kill = bool(execution.get("manual_kill_switch", False))
    status = str(runtime.get("controller_status", "UNKNOWN")).upper()
    if kill:
        return "CASHED_OUT"
    if status == "RUNNING" and not armed and not enabled:
        return "SHADOW_RUNNING"
    if status == "RUNNING" and enabled and not armed:
        return "LIVE_DEARMED"
    if status == "RUNNING" and armed and enabled:
        return "LIVE_ENABLED"
    return "UNKNOWN"


def _execution_blockers(controller: Mapping[str, Any], gates: Mapping[str, Any]) -> list[str]:
    execution = _mapping(controller.get("execution"))
    if "risk_blockers" in controller or "operator_blockers" in controller:
        blockers = [str(item) for item in controller.get("risk_blockers", ()) or ()]
        blockers.extend(str(item) for item in controller.get("operator_blockers", ()) or ())
        account = _mapping(controller.get("account_cleanliness"))
        blockers.extend(str(item) for item in account.get("blockers", ()) or ())
    else:
        blockers = [str(item) for item in controller.get("execution_blockers", ()) or ()]
    if not bool(execution.get("execution_enabled", False)):
        blockers.append("execution_disabled")
    if not bool(execution.get("mainnet_armed", False)):
        blockers.append("mainnet_not_armed")
    if bool(execution.get("manual_kill_switch", False)):
        blockers.append("manual_kill_switch_active")
    if gates.get("ready") is not True:
        blockers.extend(f"risk_gate:{item}" for item in gates.get("reasons", ()) or ())
    return list(dict.fromkeys(blockers))


def diagnostic_snapshot(payload: Any, now: float | None = None) -> dict[str, Any]:
    """Normalize one status response without inventing missing data."""

    now = time.time() if now is None else now
    root = _unwrap(payload)
    bot_status = str(root.get("status", "STOPPED")).upper()
    controller = _find_sol(root)
    runtime = _mapping(controller.get("runtime"))
    execution = _mapping(controller.get("execution"))
    perp = _mapping(controller.get("perp"))
    options = _mapping(controller.get("options"))
    iv_state = _mapping(controller.get("iv_state"))
    mode = _mapping(controller.get("mode"))
    inventory = _mapping(controller.get("inventory"))
    grid = dict(_mapping(controller.get("grid")))
    # The controller keeps the executable bounds and native minimum on the
    # selected leg. Promote those values into the normalized grid contract so
    # the live Condor table does not show N/A while a valid plan is present.
    selected_leg = _mapping(grid.get("selected_leg"))
    if selected_leg:
        grid.setdefault("lower_bound", selected_leg.get("start_price"))
        grid.setdefault("upper_bound", selected_leg.get("end_price"))
        grid.setdefault("effective_levels", selected_leg.get("max_open_orders"))
        grid.setdefault("min_order_amount_quote", selected_leg.get("min_order_amount_quote"))
        grid.setdefault("total_amount_quote", selected_leg.get("total_amount_quote"))
    gates = _mapping(controller.get("risk_gates"))
    reconciliation = _mapping(controller.get("reconciliation"))
    capital = _mapping(controller.get("capital"))
    entry_safety = _mapping(controller.get("entry_safety"))
    exit_safety = _mapping(controller.get("exit_safety"))
    fee_economics = _mapping(controller.get("fee_economics"))
    connector_proof = _mapping(controller.get("connector_proof"))
    if not connector_proof:
        connector_proof = {
            "status": "UNVERIFIED",
            "open_reduce_only": False,
            "close_reduce_only": False,
            "limit_maker_post_only": False,
            "lifecycle": "UNVERIFIED",
            "config_assertions_allowed": False,
        }
    connector_proof.setdefault("open_reduce_only", "UNVERIFIED")
    connector_proof.setdefault("limit_tif", "UNKNOWN")
    connector_proof.setdefault("limit_maker_tif", "UNKNOWN")
    connector_proof.setdefault("contract_verified", False)
    connector_proof.setdefault("lifecycle_verified", False)
    lifecycle_evidence = _mapping(controller.get("lifecycle_evidence"))
    if not lifecycle_evidence:
        lifecycle_evidence = {
            "path": connector_proof.get("lifecycle_evidence_path"),
            "present": connector_proof.get("lifecycle_evidence_present", False),
            "evidence_id": connector_proof.get("lifecycle_evidence_id"),
            "sha256": connector_proof.get("lifecycle_evidence_sha256"),
            "approved": connector_proof.get("lifecycle_evidence_approved", False),
            "generated_at": connector_proof.get("lifecycle_evidence_generated_at"),
            "evidence_runtime_instance_id": connector_proof.get(
                "lifecycle_evidence_runtime_instance_id"
            ),
            "stage_a_status": connector_proof.get("lifecycle_stage_a_status"),
            "stage_b_status": connector_proof.get("lifecycle_stage_b_status"),
            "cleanup_status": connector_proof.get("lifecycle_cleanup_status"),
            "runtime_identity_match": connector_proof.get(
                "lifecycle_runtime_identity_match", False
            ),
            "account_identity_match": connector_proof.get(
                "lifecycle_account_identity_match", False
            ),
            "runtime_instance_id": connector_proof.get("runtime_instance_id"),
            "runtime_image_ref": connector_proof.get("runtime_image_ref"),
            "account_fingerprint": connector_proof.get("runtime_account_fingerprint"),
            "runtime_contract_id": connector_proof.get("runtime_contract_id"),
            "runtime_manifest_verified": connector_proof.get(
                "runtime_manifest_verified", False
            ),
            "blockers": connector_proof.get("lifecycle_evidence_blockers")
            or ("BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE",),
            "lifecycle_verified": connector_proof.get("lifecycle_verified", False),
        }
    lifecycle_evidence.setdefault("blockers", ())
    lifecycle_evidence.setdefault("lifecycle_verified", False)
    lifecycle_evidence.setdefault("runtime_identity_match", False)
    lifecycle_evidence.setdefault("account_identity_match", False)
    if not lifecycle_evidence.get("lifecycle_verified"):
        blockers_for_lifecycle = list(lifecycle_evidence.get("blockers", ()) or ())
        blockers_for_lifecycle.append("BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE")
        lifecycle_evidence["blockers"] = tuple(dict.fromkeys(blockers_for_lifecycle))
    account_cleanliness = _mapping(controller.get("account_cleanliness"))
    if not account_cleanliness and controller:
        account_cleanliness = {
            "clean": False,
            "position_zero": None,
            "active_orders_count": None,
            "managed_executors_count": None,
            "unmanaged_executors_count": None,
            "blockers": ("account_cleanliness_not_reported_by_loaded_controller",),
        }
    live_readiness = _mapping(controller.get("live_readiness"))
    pnl = _mapping(controller.get("pnl"))
    evidence = _mapping(controller.get("evidence"))
    calibration = _mapping(controller.get("calibration_observation"))
    market_state = str(controller.get("market_state", iv_state.get("state", "UNKNOWN")))
    grid_mode = str(controller.get("grid_mode", mode.get("mode", "UNKNOWN")))
    execution_state = _execution_state(controller)
    blockers = _execution_blockers(controller, gates)
    blockers.extend(str(item) for item in lifecycle_evidence.get("blockers", ()) or ())

    alerts: list[str] = []
    if bot_status in {"STOPPED", "NOT_FOUND", "ERROR", "STOPPING"}:
        alerts.append("PROCESS_DOWN")
    if not controller:
        alerts.append("CONTROLLER_DIAGNOSTICS_MISSING")
    if controller and not bool(controller.get("perp_market_ready", False)):
        alerts.append("PERP_NOT_READY")
    if controller and not bool(controller.get("options_data_available", False)):
        alerts.append("OPTIONS_NOT_READY")
    if controller and not bool(
        controller.get("connector_ready", gates.get("connector_ready", False))
    ):
        alerts.append("CONNECTOR_NOT_READY")
    if controller and not bool(perp.get("pricing_ready", gates.get("pricing_ready", False))):
        alerts.append("PRICING_NOT_READY")
    if grid_mode not in GRID_MODES and controller:
        alerts.append("UNKNOWN_GRID_MODE")
    if market_state == "INITIALIZING":
        alerts.append("IV_STATE_WARMING_UP")
    updated_at = _number(controller.get("updated_at"))
    if updated_at is not None and updated_at > 0 and now - updated_at > 15:
        alerts.append("DIAGNOSTICS_STALE")

    data_alert = any(
        item
        in {
            "PERP_NOT_READY",
            "OPTIONS_NOT_READY",
            "CONNECTOR_NOT_READY",
            "PRICING_NOT_READY",
            "IV_STATE_WARMING_UP",
            "DIAGNOSTICS_STALE",
        }
        for item in alerts
    )
    if any(item in {"PROCESS_DOWN", "CONTROLLER_DIAGNOSTICS_MISSING"} for item in alerts):
        overall = "CRITICAL"
    elif data_alert:
        overall = "DEGRADED_DATA"
    elif execution_state == "SHADOW_RUNNING":
        overall = "HEALTHY"
    elif execution_state in {"LIVE_DEARMED", "UNKNOWN"} or not bool(
        controller.get("can_create_executor_now", False)
    ):
        overall = "EXECUTION_BLOCKED"
    else:
        overall = "HEALTHY"

    return {
        "overall": overall,
        "health_status": overall,
        "bot_status": bot_status,
        "runtime": runtime,
        "alerts": list(dict.fromkeys(alerts)),
        "execution_state": execution_state,
        "execution_mode": execution_state,
        "execution": execution,
        "execution_blockers": blockers,
        "connector_ready": bool(
            controller.get("connector_ready", gates.get("connector_ready", False))
        ),
        "perp_ready": bool(controller.get("perp_market_ready", False)),
        "options_ready": bool(controller.get("options_data_available", False)),
        "perp": perp,
        "pricing": perp,
        "options": options,
        "iv_state": iv_state,
        "state_policy": _mapping(controller.get("state_policy")),
        "market_state": market_state,
        "grid_mode": grid_mode,
        "mode": mode,
        "mode_reasons": list(controller.get("mode_reasons", mode.get("reasons", ())) or ()),
        "risk_blockers": list(controller.get("risk_blockers", gates.get("reasons", ())) or ()),
        "operator_blockers": list(controller.get("operator_blockers", ()) or ()),
        "intended_grid_available": bool(
            controller.get("intended_grid_available", grid.get("valid", False))
        ),
        "intended_grid_mode": controller.get("intended_grid_mode", grid_mode),
        "can_create_executor_now": bool(controller.get("can_create_executor_now", False)),
        "grid": grid,
        "risk_gates": gates,
        "inventory": inventory,
        "capital": capital,
        "entry_safety": entry_safety,
        "exit_safety": exit_safety,
        "fee_economics": fee_economics,
        "connector_proof": connector_proof,
        "lifecycle_evidence": lifecycle_evidence,
        "account_cleanliness": account_cleanliness,
        "live_readiness": live_readiness,
        "reconciliation": reconciliation,
        "executors": controller.get("executors", []),
        "pnl": pnl,
        "evidence": evidence,
        "calibration_observation_ready": bool(
            controller.get("calibration_observation_ready", False)
        ),
        "calibration_observation": calibration,
        "calibration_observation_errors": controller.get("calibration_observation_errors", []),
        "errors": controller.get("errors", []),
    }


def _value(row: Mapping[str, Any], key: str, default: str = "N/A") -> str:
    value = row.get(key, default)
    return default if value is None else str(value)


def _table_row(snapshot: Mapping[str, Any]) -> dict[str, str]:
    perp = _mapping(snapshot.get("perp"))
    options = _mapping(snapshot.get("options"))
    iv_state = _mapping(snapshot.get("iv_state"))
    inventory = _mapping(snapshot.get("inventory"))
    grid = _mapping(snapshot.get("grid"))
    gates = _mapping(snapshot.get("risk_gates"))
    capital = _mapping(snapshot.get("capital"))
    entry_safety = _mapping(snapshot.get("entry_safety"))
    exit_safety = _mapping(snapshot.get("exit_safety"))
    fee_economics = _mapping(snapshot.get("fee_economics"))
    connector_proof = _mapping(snapshot.get("connector_proof"))
    connector_open = _value(connector_proof, "open_reduce_only", "UNVERIFIED")
    connector_close = _value(connector_proof, "close_reduce_only", "UNVERIFIED")
    connector_post_only = _value(connector_proof, "limit_maker_post_only", "UNVERIFIED")
    connector_limit_tif = _value(connector_proof, "limit_tif", "UNKNOWN")
    connector_maker_tif = _value(connector_proof, "limit_maker_tif", "UNKNOWN")
    connector_contract = _value(connector_proof, "contract_verified", "UNKNOWN")
    connector_lifecycle = _value(connector_proof, "lifecycle", "UNVERIFIED")
    lifecycle_evidence = _mapping(snapshot.get("lifecycle_evidence"))
    fee_status = _value(fee_economics, "fee_model_status", "UNKNOWN")
    economic_floor = _value(
        fee_economics, "minimum_economic_take_profit_pct", "UNKNOWN"
    )
    raw_maker = _value(fee_economics, "raw_hummingbot_maker_fee", "UNKNOWN")
    raw_taker = _value(fee_economics, "raw_hummingbot_taker_fee", "UNKNOWN")
    normalized_maker = _value(fee_economics, "normalized_maker_fee_decimal", "UNKNOWN")
    normalized_taker = _value(fee_economics, "normalized_taker_fee_decimal", "UNKNOWN")
    normal_round_trip = _value(fee_economics, "normal_round_trip_fee_decimal", "UNKNOWN")
    emergency_round_trip = _value(
        fee_economics, "emergency_round_trip_fee_decimal", "UNKNOWN"
    )
    configured_tp = _value(fee_economics, "configured_take_profit_pct", "UNKNOWN")
    account_cleanliness = _mapping(snapshot.get("account_cleanliness"))
    live_readiness = _mapping(snapshot.get("live_readiness"))
    reconciliation = _mapping(snapshot.get("reconciliation"))
    pnl = _mapping(snapshot.get("pnl"))
    return {
        "Asset": "SOL",
        "Execution": _value(snapshot, "execution_state"),
        "BBO bid/ask/mid": (
            f"{_value(perp, 'bid')} / {_value(perp, 'ask')} / {_value(perp, 'mid')}"
        ),
        "Spread bps / age": f"{_value(perp, 'spread_bps')} / {_value(perp, 'age_seconds')}",
        "IV / baseline / ratio": (
            f"{_value(iv_state, 'current_iv')} / "
            f"{_value(iv_state, 'baseline_iv')} / {_value(iv_state, 'iv_ratio')}"
        ),
        "State / intended mode": (
            f"{_value(snapshot, 'market_state')} / {_value(snapshot, 'intended_grid_mode')}"
        ),
        "Expiry / DTE / strike": (
            f"{_value(options, 'expiry')} / {_value(options, 'days_to_expiry')} / "
            f"{_value(options, 'atm_strike')}"
        ),
        "Position / collateral": (
            f"{_value(inventory, 'position_quote')} / "
            f"{_value(inventory, 'available_collateral_quote')}"
        ),
        "Grid bounds / levels": (
            f"{_value(grid, 'lower_bound')} / {_value(grid, 'upper_bound')} / "
            f"{_value(grid, 'effective_levels')}"
        ),
        "Risk / create now": (
            f"{_value(gates, 'ready')} / {_value(snapshot, 'can_create_executor_now')}"
        ),
        "Capital available / target / deployable": (
            f"{_value(capital, 'available_collateral_quote')} / "
            f"{_value(capital, 'target_strategy_quote')} / "
            f"{_value(capital, 'deployable_quote')}"
        ),
        "Entry / exit / fee": (
            f"{_value(entry_safety, 'semantics_ready')} / "
            f"{_value(exit_safety, 'semantics_ready')} / "
            f"{_value(fee_economics, 'ready')}"
        ),
        "Connector contract": (
            f"open_reduce_only={connector_open} / close={connector_close} / "
            f"limit_tif={connector_limit_tif} / maker_tif={connector_maker_tif} / "
            f"contract={connector_contract} / post_only={connector_post_only} / "
            f"lifecycle={connector_lifecycle}"
        ),
        "Lifecycle evidence": (
            f"verified={_value(lifecycle_evidence, 'lifecycle_verified', 'False')} / "
            f"approved={_value(lifecycle_evidence, 'approved', 'False')} / "
            f"runtime_match={_value(lifecycle_evidence, 'runtime_identity_match', 'False')} / "
            f"account_match={_value(lifecycle_evidence, 'account_identity_match', 'False')} / "
            f"A={_value(lifecycle_evidence, 'stage_a_status', 'UNKNOWN')} / "
            f"B={_value(lifecycle_evidence, 'stage_b_status', 'UNKNOWN')} / "
            f"cleanup={_value(lifecycle_evidence, 'cleanup_status', 'UNKNOWN')} / "
            f"runtime={_value(lifecycle_evidence, 'runtime_contract_id', 'UNKNOWN')} / "
            "blockers="
            + (
                ",".join(str(item) for item in lifecycle_evidence.get("blockers", ()) or ())
                or "None"
            )
        ),
        "Fee model / economic TP floor": (
            f"{fee_status} / {economic_floor}"
        ),
        "Fees raw / normalized / configured TP": (
            f"{raw_maker}/{raw_taker} / {normalized_maker}/{normalized_taker} / {configured_tp}"
        ),
        "Fee round trips": f"normal={normal_round_trip} / emergency={emergency_round_trip}",
        "Account clean / live readiness": (
            f"{_value(account_cleanliness, 'clean')} / "
            f"{_value(live_readiness, 'status')}"
        ),
        "Account/runtime binding": (
            f"fingerprint={_value(account_cleanliness, 'account_fingerprint')} / "
            f"instance={_value(lifecycle_evidence, 'runtime_instance_id')} / "
            f"image={_value(lifecycle_evidence, 'runtime_image_ref')}"
        ),
        "Keep / stop / create": (
            f"{_value(reconciliation, 'kept')} / "
            f"{_value(reconciliation, 'stops')} / "
            f"{_value(reconciliation, 'creates')}"
        ),
        "Real fills / PnL": (
            f"{_value(pnl, 'real_executor_fill_count')} / "
            f"{_value(pnl, 'realized_pnl_quote')} / "
            f"{_value(pnl, 'unrealized_pnl_quote')}"
        ),
    }


def render_text(snapshot: Mapping[str, Any]) -> str:
    """Compact English rendering used by tests and non-UI callers."""

    perp = _mapping(snapshot.get("perp"))
    iv_state = _mapping(snapshot.get("iv_state"))
    gates = _mapping(snapshot.get("risk_gates"))
    capital = _mapping(snapshot.get("capital"))
    entry_safety = _mapping(snapshot.get("entry_safety"))
    exit_safety = _mapping(snapshot.get("exit_safety"))
    fee_economics = _mapping(snapshot.get("fee_economics"))
    connector_proof = _mapping(snapshot.get("connector_proof"))
    connector_open = _value(connector_proof, "open_reduce_only", "UNVERIFIED")
    connector_close = _value(connector_proof, "close_reduce_only", "UNVERIFIED")
    connector_post_only = _value(connector_proof, "limit_maker_post_only", "UNVERIFIED")
    connector_limit_tif = _value(connector_proof, "limit_tif", "UNKNOWN")
    connector_maker_tif = _value(connector_proof, "limit_maker_tif", "UNKNOWN")
    connector_contract = _value(connector_proof, "contract_verified", "UNKNOWN")
    connector_lifecycle = _value(connector_proof, "lifecycle", "UNVERIFIED")
    connector_status = _value(connector_proof, "status", "UNVERIFIED")
    lifecycle_evidence = _mapping(snapshot.get("lifecycle_evidence"))
    fee_status = _value(fee_economics, "fee_model_status", "UNKNOWN")
    schema_maker = _value(fee_economics, "schema_maker_fee_pct", "UNKNOWN")
    schema_taker = _value(fee_economics, "schema_taker_fee_pct", "UNKNOWN")
    metadata_maker = _value(
        fee_economics, "instrument_metadata_maker_fee_pct", "UNKNOWN"
    )
    metadata_taker = _value(
        fee_economics, "instrument_metadata_taker_fee_pct", "UNKNOWN"
    )
    raw_maker = _value(fee_economics, "raw_hummingbot_maker_fee", schema_maker)
    raw_taker = _value(fee_economics, "raw_hummingbot_taker_fee", schema_taker)
    normalized_maker = _value(fee_economics, "normalized_maker_fee_decimal", "UNKNOWN")
    normalized_taker = _value(fee_economics, "normalized_taker_fee_decimal", "UNKNOWN")
    normal_round_trip = _value(fee_economics, "normal_round_trip_fee_decimal", "UNKNOWN")
    emergency_round_trip = _value(
        fee_economics, "emergency_round_trip_fee_decimal", "UNKNOWN"
    )
    configured_tp = _value(fee_economics, "configured_take_profit_pct", "UNKNOWN")
    economic_floor = _value(
        fee_economics, "minimum_economic_take_profit_pct", "UNKNOWN"
    )
    account_cleanliness = _mapping(snapshot.get("account_cleanliness"))
    live_readiness = _mapping(snapshot.get("live_readiness"))
    return "\n".join(
        [
            f"Overall: {_value(snapshot, 'overall')}",
            f"Bot: {_value(snapshot, 'bot_status')} / "
            f"Controller: {_value(snapshot.get('runtime', {}), 'controller_status')}",
            f"Execution state: {_value(snapshot, 'execution_state')}",
            f"PERP_READY: {snapshot.get('perp_ready', False)}",
            f"OPTIONS_READY: {snapshot.get('options_ready', False)}",
            f"CONNECTOR_READY: {snapshot.get('connector_ready', False)}",
            f"CALIBRATION_CAPTURE_READY: {snapshot.get('calibration_observation_ready', False)}",
            f"BBO bid / ask / mid: {_value(perp, 'bid')} / "
            f"{_value(perp, 'ask')} / {_value(perp, 'mid')}",
            f"IV / baseline / ratio: {_value(iv_state, 'current_iv')} / "
            f"{_value(iv_state, 'baseline_iv')} / {_value(iv_state, 'iv_ratio')}",
            f"Market state / intended grid mode: {_value(snapshot, 'market_state')} / "
            f"{_value(snapshot, 'intended_grid_mode')}",
            f"State / mode: {_value(snapshot, 'market_state')} / {_value(snapshot, 'grid_mode')}",
            f"State ready / execution ready: {_value(gates, 'state_ready')} / "
            f"{_value(snapshot, 'can_create_executor_now')}",
            f"Capital available / target / deployable: "
            f"{_value(capital, 'available_collateral_quote')} / "
            f"{_value(capital, 'target_strategy_quote')} / "
            f"{_value(capital, 'deployable_quote')}",
            f"Entry / exit / fee ready: {_value(entry_safety, 'semantics_ready')} / "
            f"{_value(exit_safety, 'semantics_ready')} / {_value(fee_economics, 'ready')}",
            "Connector contract: "
            f"close_reduce_only={connector_close} / "
            f"open_reduce_only={connector_open} / "
            f"close_reduce_only={connector_close} / "
            f"limit_tif={connector_limit_tif} / "
            f"limit_maker_tif={connector_maker_tif} / "
            f"contract_verified={connector_contract} / "
            f"limit_maker_post_only={connector_post_only} / "
            f"lifecycle={connector_lifecycle} / "
            f"status={connector_status}",
            "Lifecycle evidence: "
            f"verified={_value(lifecycle_evidence, 'lifecycle_verified', 'False')} / "
            f"approved={_value(lifecycle_evidence, 'approved', 'False')} / "
            f"present={_value(lifecycle_evidence, 'present', 'False')} / "
            f"runtime_match={_value(lifecycle_evidence, 'runtime_identity_match', 'False')} / "
            f"account_match={_value(lifecycle_evidence, 'account_identity_match', 'False')} / "
            f"A={_value(lifecycle_evidence, 'stage_a_status', 'UNKNOWN')} / "
            f"B={_value(lifecycle_evidence, 'stage_b_status', 'UNKNOWN')} / "
            f"cleanup={_value(lifecycle_evidence, 'cleanup_status', 'UNKNOWN')} / "
            f"runtime={_value(lifecycle_evidence, 'runtime_contract_id', 'UNKNOWN')} / "
            "blockers="
            + (
                ", ".join(str(item) for item in lifecycle_evidence.get("blockers", ()) or ())
                or "None"
            ),
            f"Connector contract legacy close_reduce_only={connector_close}",
            "Fee model: "
            f"status={fee_status} / "
            f"Hummingbot maker/taker={schema_maker} / {schema_taker} / "
            f"raw={raw_maker} / {raw_taker} / "
            f"normalized={normalized_maker} / {normalized_taker} / "
            f"Derive maker/taker={metadata_maker} / {metadata_taker} / "
            f"normal RT={normal_round_trip} / emergency RT={emergency_round_trip} / "
            f"configured TP={configured_tp} / economic TP floor={economic_floor}",
            f"Account clean / live readiness: {_value(account_cleanliness, 'clean')} / "
            f"{_value(live_readiness, 'status')}",
            "Account/runtime binding: "
            f"fingerprint={_value(account_cleanliness, 'account_fingerprint')} / "
            f"instance={_value(lifecycle_evidence, 'runtime_instance_id')} / "
            f"image={_value(lifecycle_evidence, 'runtime_image_ref')}",
            "Mode reasons: "
            + (", ".join(str(item) for item in snapshot.get("mode_reasons", ())) or "None"),
            "Risk blockers: "
            + (", ".join(str(item) for item in snapshot.get("risk_blockers", ())) or "None"),
            "Blockers: "
            + (", ".join(str(item) for item in snapshot.get("execution_blockers", ())) or "None"),
            f"Alerts: {', '.join(str(item) for item in snapshot.get('alerts', ())) or 'None'}",
        ]
    )


async def run(config: Config, context: Any) -> str:
    """Continuously render Hummingbot diagnostics; never mutate trading state."""

    if config.execution_enabled:
        raise RuntimeError("derive_options_grid_health refused execution_enabled=true")
    from config_manager import get_client

    from condor.reports import LiveReport

    chat_id = getattr(context, "_chat_id", None)
    report = LiveReport(
        "Derive SOL Options Adaptive Grid Health",
        source_name="derive_options_grid_health",
        tags=["derive", "sol", "options", "grid", "read-only"],
        auto_refresh_seconds=config.report_auto_refresh_seconds,
    )
    ticks = 0
    try:
        while True:
            client = await get_client(chat_id, context=context)
            payload: Any = {"status": "STOPPED", "error_logs": []}
            if client is not None:
                try:
                    payload = await client.bot_orchestration.get_bot_status(config.bot_name)
                except Exception as exc:
                    payload = {
                        "status": "ERROR",
                        "error_logs": [{"msg": f"{type(exc).__name__}: {exc}"}],
                    }
            snapshot = diagnostic_snapshot(payload)
            execution = _mapping(snapshot["execution"])
            perp = _mapping(snapshot["perp"])
            options = _mapping(snapshot["options"])
            iv_state = _mapping(snapshot["iv_state"])
            gates = _mapping(snapshot["risk_gates"])
            inventory = _mapping(snapshot["inventory"])
            capital = _mapping(snapshot["capital"])
            entry_safety = _mapping(snapshot["entry_safety"])
            exit_safety = _mapping(snapshot["exit_safety"])
            fee_economics = _mapping(snapshot["fee_economics"])
            connector_proof = _mapping(snapshot["connector_proof"])
            connector_open = _value(connector_proof, "open_reduce_only", "UNVERIFIED")
            connector_status = _value(connector_proof, "status", "UNVERIFIED")
            connector_close = _value(connector_proof, "close_reduce_only", "UNVERIFIED")
            connector_post_only = _value(
                connector_proof, "limit_maker_post_only", "UNVERIFIED"
            )
            connector_limit_tif = _value(connector_proof, "limit_tif", "UNKNOWN")
            connector_maker_tif = _value(connector_proof, "limit_maker_tif", "UNKNOWN")
            connector_contract = _value(connector_proof, "contract_verified", "UNKNOWN")
            connector_lifecycle = _value(connector_proof, "lifecycle", "UNVERIFIED")
            lifecycle_evidence = _mapping(snapshot["lifecycle_evidence"])
            fee_status = _value(fee_economics, "fee_model_status", "UNKNOWN")
            schema_maker = _value(fee_economics, "schema_maker_fee_pct", "UNKNOWN")
            schema_taker = _value(fee_economics, "schema_taker_fee_pct", "UNKNOWN")
            metadata_maker = _value(
                fee_economics, "instrument_metadata_maker_fee_pct", "UNKNOWN"
            )
            metadata_taker = _value(
                fee_economics, "instrument_metadata_taker_fee_pct", "UNKNOWN"
            )
            raw_maker = _value(fee_economics, "raw_hummingbot_maker_fee", schema_maker)
            raw_taker = _value(fee_economics, "raw_hummingbot_taker_fee", schema_taker)
            normalized_maker = _value(
                fee_economics, "normalized_maker_fee_decimal", "UNKNOWN"
            )
            normalized_taker = _value(
                fee_economics, "normalized_taker_fee_decimal", "UNKNOWN"
            )
            normal_round_trip = _value(
                fee_economics, "normal_round_trip_fee_decimal", "UNKNOWN"
            )
            emergency_round_trip = _value(
                fee_economics, "emergency_round_trip_fee_decimal", "UNKNOWN"
            )
            configured_tp = _value(
                fee_economics, "configured_take_profit_pct", "UNKNOWN"
            )
            economic_floor = _value(
                fee_economics, "minimum_economic_take_profit_pct", "UNKNOWN"
            )
            account_cleanliness = _mapping(snapshot["account_cleanliness"])
            live_readiness = _mapping(snapshot["live_readiness"])
            report.clear()
            report.builder.manual_order()
            report.builder.kpi("Health", snapshot["overall"])
            report.builder.kpi(
                "Bot / controller",
                f"{snapshot['bot_status']} / {_value(snapshot['runtime'], 'controller_status')}",
            )
            report.builder.kpi("Execution state", snapshot["execution_state"])
            report.builder.kpi(
                "Armed / enabled / kill",
                f"{_value(execution, 'mainnet_armed')} / "
                f"{_value(execution, 'execution_enabled')} / "
                f"{_value(execution, 'manual_kill_switch')}",
            )
            report.builder.kpi(
                "BBO bid / ask / mid",
                f"{_value(perp, 'bid')} / {_value(perp, 'ask')} / {_value(perp, 'mid')}",
            )
            report.builder.kpi(
                "Spread bps / age", f"{_value(perp, 'spread_bps')} / {_value(perp, 'age_seconds')}"
            )
            report.builder.kpi("Pricing", "READY" if perp.get("pricing_ready") else "BLOCKED")
            report.builder.kpi(
                "Call / put / ATM IV",
                f"{_value(options, 'call_iv')} / {_value(options, 'put_iv')} / "
                f"{_value(options, 'atm_iv')}",
            )
            report.builder.kpi("IV ratio", _value(iv_state, "iv_ratio"))
            report.builder.kpi(
                "Market state / grid mode", f"{snapshot['market_state']} / {snapshot['grid_mode']}"
            )
            report.builder.kpi(
                "Intended grid / create now",
                f"{snapshot['intended_grid_available']} / {snapshot['can_create_executor_now']}",
            )
            report.builder.kpi(
                "Position / collateral",
                f"{_value(inventory, 'position_quote')} / "
                f"{_value(inventory, 'available_collateral_quote')}",
            )
            report.builder.kpi(
                "Capital available / reserve / deployable",
                f"{_value(capital, 'available_collateral_quote')} / "
                f"{_value(capital, 'reserve_quote')} / "
                f"{_value(capital, 'deployable_quote')}",
            )
            report.builder.kpi(
                "Capital target / fee buffer / ready",
                f"{_value(capital, 'target_strategy_quote')} / "
                f"{_value(capital, 'fee_buffer_quote')} / "
                f"{_value(capital, 'capital_ready')}",
            )
            report.builder.kpi(
                "Entry / exit / fee economics",
                f"{_value(entry_safety, 'semantics_ready')} / "
                f"{_value(exit_safety, 'semantics_ready')} / "
                f"{_value(fee_economics, 'ready')}",
            )
            report.builder.kpi(
                "Connector contract",
                f"open={connector_open} / close={connector_close} / "
                f"limit_tif={connector_limit_tif} / maker_tif={connector_maker_tif} / "
                f"contract={connector_contract} / post_only={connector_post_only} / "
                f"lifecycle={connector_lifecycle}",
            )
            report.builder.kpi(
                "Lifecycle evidence",
                f"verified={_value(lifecycle_evidence, 'lifecycle_verified', 'False')} / "
                f"approved={_value(lifecycle_evidence, 'approved', 'False')} / "
                f"runtime={_value(lifecycle_evidence, 'runtime_contract_id', 'UNKNOWN')}",
            )
            report.builder.kpi(
                "Lifecycle blockers",
                ", ".join(str(item) for item in lifecycle_evidence.get("blockers", ()) or ())
                or "None",
            )
            report.builder.kpi(
                "Fee model / economic TP floor",
                f"{fee_status} / {economic_floor}",
            )
            report.builder.kpi(
                "Fees raw / normalized",
                f"{raw_maker}/{raw_taker} / {normalized_maker}/{normalized_taker}",
            )
            report.builder.kpi(
                "Fee round trips / configured TP",
                f"{normal_round_trip} / {emergency_round_trip} / {configured_tp}",
            )
            report.builder.kpi(
                "Account clean / live readiness",
                f"{_value(account_cleanliness, 'clean')} / "
                f"{_value(live_readiness, 'status')}",
            )
            report.builder.kpi("Risk gates", "READY" if gates.get("ready") else "BLOCKED")
            report.builder.kpi("Real fills", _value(snapshot["pnl"], "real_executor_fill_count"))
            report.builder.table([_table_row(snapshot)])
            report.builder.markdown(
                "## Safety gates\n"
                + "\n".join(
                    f"- {key}: {_value(gates, key)}"
                    for key in (
                        "market_data_ready",
                        "options_ready",
                        "state_ready",
                        "pricing_ready",
                        "collateral_ready",
                        "inventory_ready",
                        "executor_ready",
                        "connector_ready",
                        "grid_size_ready",
                        "capital_ready",
                        "fee_economics_ready",
                        "entry_semantics_ready",
                        "exit_semantics_ready",
                        "lifecycle_ready",
                        "manual_kill_clear",
                        "hard_block",
                        "planning_ready",
                        "ready",
                    )
                )
                + "\n\n## Execution blockers\n"
                + ("\n".join(f"- {item}" for item in snapshot["execution_blockers"]) or "- None")
                + "\n\n## Mode reasons (informational)\n"
                + ("\n".join(f"- {item}" for item in snapshot["mode_reasons"]) or "- None")
                + "\n\n## Account cleanliness\n"
                + "\n".join(
                    f"- {key}: {_value(account_cleanliness, key)}"
                    for key in (
                        "position_zero",
                        "active_orders_count",
                        "managed_executors_count",
                        "unmanaged_executors_count",
                        "clean",
                    )
                )
                + "\n\n## Connector and fee evidence\n"
                + f"- Connector contract: {connector_status}\n"
                + f"- OPEN reduce-only: {connector_open}\n"
                + f"- CLOSE reduce-only: {connector_close}\n"
                + f"- LIMIT time-in-force: {connector_limit_tif}\n"
                + f"- LIMIT_MAKER post-only: {connector_post_only}\n"
                + f"- LIMIT_MAKER time-in-force: {connector_maker_tif}\n"
                + f"- Static contract verified: {connector_contract}\n"
                + f"- Lifecycle: {connector_lifecycle}\n"
                + f"- Fee model: {fee_status}\n"
                + f"- Hummingbot raw maker/taker: {raw_maker} / {raw_taker}\n"
                + f"- Hummingbot normalized maker/taker: {normalized_maker} / {normalized_taker}\n"
                + f"- Derive metadata maker/taker: {metadata_maker} / {metadata_taker}\n"
                + f"- Normal/emergency round trip: {normal_round_trip} / {emergency_round_trip}\n"
                + f"- Configured take-profit: {configured_tp}\n"
                + f"- Economic take-profit floor: {economic_floor}"
            )
            report.builder.markdown(
                "## Read-only boundary\n"
                "- Condor reads Hummingbot status only.\n"
                "- `SHADOW_RUNNING` means the controller is RUNNING with all execution gates off.\n"
                "- Shadow plans and proxy touches are not executor fills."
            )
            await report.update()
            ticks += 1
            await asyncio.sleep(config.poll_interval_seconds)
    except asyncio.CancelledError:
        return f"derive_options_grid_health stopped after {ticks} read-only updates"


__all__ = [
    "ASSETS",
    "CATEGORY",
    "CONTINUOUS",
    "Config",
    "diagnostic_snapshot",
    "render_text",
    "run",
]
