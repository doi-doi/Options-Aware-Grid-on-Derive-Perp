# DERIVE_OPTIONS_ADAPTIVE_GRID_CONDOR_MANAGED
"""Continuous, read-only persistence of the controller's shadow observations.

The controller owns causal observation construction.  This routine only reads
``bot_orchestration.get_bot_status()``, checks the disarmed execution gates,
and appends already-validated observation mappings to one JSONL file.  It has
no configuration, executor, order, cancellation, connector, or account
mutation surface.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

CATEGORY = "Monitoring"
CONTINUOUS = True
ASSETS = ("SOL",)


class Config(BaseModel):
    bot_name: str = Field(default="derive-options-adaptive-grid-sol-three-mode-shadow")
    output_path: str = Field(
        default="data/derive_options_adaptive_grid/observations.jsonl",
        min_length=1,
        description="Persistent JSONL path; use an explicit runtime-mounted path.",
    )
    poll_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    report_auto_refresh_seconds: int = Field(default=5, ge=1, le=300)
    execution_enabled: bool = False

    @model_validator(mode="after")
    def read_only(self):
        if self.execution_enabled:
            raise ValueError("derive_options_grid_shadow_capture is read-only")
        return self


@dataclass(slots=True)
class CaptureCounters:
    output_path: str
    records_written: int = 0
    duplicates_skipped: int = 0
    invalid_or_unready_skipped: int = 0
    unsafe_gate_skipped: int = 0
    last_decision_timestamp: float | None = None
    last_source_timestamp: float | None = None
    last_market_state: str | None = None
    last_grid_mode: str | None = None
    last_iv_ratio: float | None = None
    last_controller_status: str | None = None
    last_bbo_marker: Any = None
    last_bid: float | None = None
    last_ask: float | None = None
    last_mid: float | None = None
    last_available_collateral: float | None = None
    last_deployable_capital: float | None = None
    last_target_capital: float | None = None
    last_fee_buffer: float | None = None
    last_capital_ready: bool | None = None
    last_entry_safety_ready: bool | None = None
    last_exit_safety_ready: bool | None = None
    last_fee_economics_ready: bool | None = None
    last_fee_model_status: str | None = None
    last_fee_source_mismatch: bool | None = None
    last_connector_proof_status: str | None = None
    last_connector_contract_verified: bool | None = None
    last_connector_open_reduce_only: bool | None = None
    last_connector_close_reduce_only: bool | None = None
    last_connector_post_only: bool | None = None
    last_connector_limit_tif: str | None = None
    last_connector_maker_tif: str | None = None
    last_connector_lifecycle: str | None = None
    last_lifecycle_evidence_present: bool | None = None
    last_lifecycle_evidence_approved: bool | None = None
    last_lifecycle_evidence_id: str | None = None
    last_lifecycle_evidence_sha256: str | None = None
    last_runtime_contract_id: str | None = None
    last_runtime_instance_id: str | None = None
    last_runtime_image_ref: str | None = None
    last_account_fingerprint: str | None = None
    last_runtime_manifest_verified: bool | None = None
    last_lifecycle_evidence_blockers: list[str] | None = None
    last_live_readiness: str | None = None
    last_account_clean: bool | None = None
    last_execution_blockers: list[str] | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "records_written": self.records_written,
            "duplicates_skipped": self.duplicates_skipped,
            "invalid_or_unready_skipped": self.invalid_or_unready_skipped,
            "unsafe_gate_skipped": self.unsafe_gate_skipped,
            "last_decision_timestamp": self.last_decision_timestamp,
            "last_source_timestamp": self.last_source_timestamp,
            "last_market_state": self.last_market_state,
            "last_grid_mode": self.last_grid_mode,
            "last_iv_ratio": self.last_iv_ratio,
            "last_controller_status": self.last_controller_status,
            "last_bbo_marker": self.last_bbo_marker,
            "last_bid": self.last_bid,
            "last_ask": self.last_ask,
            "last_mid": self.last_mid,
            "last_available_collateral": self.last_available_collateral,
            "last_deployable_capital": self.last_deployable_capital,
            "last_target_capital": self.last_target_capital,
            "last_fee_buffer": self.last_fee_buffer,
            "last_capital_ready": self.last_capital_ready,
            "last_entry_safety_ready": self.last_entry_safety_ready,
            "last_exit_safety_ready": self.last_exit_safety_ready,
            "last_fee_economics_ready": self.last_fee_economics_ready,
            "last_fee_model_status": self.last_fee_model_status,
            "last_fee_source_mismatch": self.last_fee_source_mismatch,
            "last_connector_proof_status": self.last_connector_proof_status,
            "last_connector_contract_verified": self.last_connector_contract_verified,
            "last_connector_open_reduce_only": self.last_connector_open_reduce_only,
            "last_connector_close_reduce_only": self.last_connector_close_reduce_only,
            "last_connector_post_only": self.last_connector_post_only,
            "last_connector_limit_tif": self.last_connector_limit_tif,
            "last_connector_maker_tif": self.last_connector_maker_tif,
            "last_connector_lifecycle": self.last_connector_lifecycle,
            "last_lifecycle_evidence_present": self.last_lifecycle_evidence_present,
            "last_lifecycle_evidence_approved": self.last_lifecycle_evidence_approved,
            "last_lifecycle_evidence_id": self.last_lifecycle_evidence_id,
            "last_lifecycle_evidence_sha256": self.last_lifecycle_evidence_sha256,
            "last_runtime_contract_id": self.last_runtime_contract_id,
            "last_runtime_instance_id": self.last_runtime_instance_id,
            "last_runtime_image_ref": self.last_runtime_image_ref,
            "last_account_fingerprint": self.last_account_fingerprint,
            "last_runtime_manifest_verified": self.last_runtime_manifest_verified,
            "last_lifecycle_evidence_blockers": self.last_lifecycle_evidence_blockers,
            "last_live_readiness": self.last_live_readiness,
            "last_account_clean": self.last_account_clean,
            "last_execution_blockers": self.last_execution_blockers,
            "last_error": self.last_error,
            "evidence_policy": (
                "SHADOW_PLAN is non-execution evidence; records_written != real fills"
            ),
        }


class CaptureFileError(RuntimeError):
    """The existing capture file cannot establish a safe append boundary."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = str(value).strip()
    return parsed or None


def _unwrap(payload: Any) -> Mapping[str, Any]:
    root = _mapping(payload)
    if str(root.get("status", "")).lower() == "success" and isinstance(root.get("data"), Mapping):
        return _mapping(root["data"])
    return root


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


def observation_validation_errors(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Defensively check the capture envelope without duplicating calibration code."""

    errors: list[str] = []
    required = (
        "decision_timestamp",
        "source_timestamp",
        "received_timestamp",
        "underlying",
        "trading_pair",
        "exchange_instrument",
        "environment",
        "perp_mid",
        "best_bid",
        "best_ask",
        "atm_call_iv",
        "atm_put_iv",
        "atm_iv",
        "expiry_timestamp",
        "days_to_expiry",
        "atm_strike",
        "call_strike",
        "put_strike",
        "call_instrument",
        "put_instrument",
        "call_iv_source",
        "put_iv_source",
        "option_reference_price",
        "source",
        "evidence",
    )
    errors.extend(f"{key}_missing" for key in required if _text(row.get(key)) is None)
    if errors:
        return tuple(dict.fromkeys(errors))

    timestamps = {
        key: _number(row.get(key))
        for key in ("source_timestamp", "received_timestamp", "decision_timestamp")
    }
    if any(value is None for value in timestamps.values()):
        errors.append("timestamps_invalid")
    else:
        source = timestamps["source_timestamp"]
        received = timestamps["received_timestamp"]
        decision = timestamps["decision_timestamp"]
        assert source is not None and received is not None and decision is not None
        if source > received:
            errors.append("source_after_receipt")
        if received > decision:
            errors.append("receipt_after_decision")
        if source > decision:
            errors.append("source_after_decision")

    identity = {
        "underlying": "SOL",
        "trading_pair": "SOL-USDC",
        "exchange_instrument": "SOL-PERP",
        "environment": "mainnet",
    }
    for key, expected in identity.items():
        if str(row.get(key, "")).strip().upper() != expected.upper():
            errors.append(f"{key}_mismatch")
    if str(row.get("evidence", "")).strip().upper() != "SHADOW_PLAN":
        errors.append("evidence_must_be_SHADOW_PLAN")
    if _text(row.get("native_order_id")) is not None:
        errors.append("native_order_id_forbidden")

    positive_fields = (
        "perp_mid",
        "best_bid",
        "best_ask",
        "atm_call_iv",
        "atm_put_iv",
        "atm_iv",
        "expiry_timestamp",
        "days_to_expiry",
        "atm_strike",
        "call_strike",
        "put_strike",
        "option_reference_price",
    )
    values = {key: _number(row.get(key)) for key in positive_fields}
    errors.extend(f"{key}_invalid" for key, value in values.items() if value is None)
    if all(values[key] is not None for key in positive_fields):
        assert all(values[key] is not None for key in positive_fields)
        if values["best_ask"] <= values["best_bid"]:
            errors.append("best_ask_not_above_best_bid")
        expected_mid = (values["best_bid"] + values["best_ask"]) / 2.0
        if not math.isclose(values["perp_mid"], expected_mid, rel_tol=0, abs_tol=1e-12):
            errors.append("perp_mid_not_exact_bbo_midpoint")
        if not math.isclose(
            values["option_reference_price"], values["perp_mid"], rel_tol=0, abs_tol=1e-12
        ):
            errors.append("option_reference_price_not_bbo_midpoint")
        if not 2.0 <= values["days_to_expiry"] <= 14.0:
            errors.append("days_to_expiry_outside_2_to_14")
        if not math.isclose(values["atm_iv"], (values["atm_call_iv"] + values["atm_put_iv"]) / 2.0):
            errors.append("atm_iv_not_call_put_mean")
        if not (
            math.isclose(values["atm_strike"], values["call_strike"], rel_tol=1e-9, abs_tol=1e-9)
            and math.isclose(values["atm_strike"], values["put_strike"], rel_tol=1e-9, abs_tol=1e-9)
        ):
            errors.append("selected_strikes_mismatch")
        if (
            abs(values["atm_strike"] - values["option_reference_price"])
            / values["option_reference_price"]
            > 0.05
        ):
            errors.append("atm_distance_above_5pct")
        decision = (
            values["decision_timestamp"]
            if "decision_timestamp" in values
            else _number(row.get("decision_timestamp"))
        )
        if decision is not None and values["expiry_timestamp"] <= decision:
            errors.append("expiry_must_be_after_decision")

    return tuple(dict.fromkeys(errors))


def _ordering_errors(
    row: Mapping[str, Any],
    last_source_timestamp: float | None,
    last_decision_timestamp: float | None,
) -> tuple[str, ...]:
    source = _number(row.get("source_timestamp"))
    decision = _number(row.get("decision_timestamp"))
    if source is None or decision is None:
        return ("timestamps_invalid",)
    if last_source_timestamp is None or last_decision_timestamp is None:
        return ()
    if source == last_source_timestamp and decision == last_decision_timestamp:
        return ("duplicate_observation",)
    errors: list[str] = []
    if source <= last_source_timestamp:
        errors.append("source_timestamp_not_strictly_increasing")
    if decision <= last_decision_timestamp:
        errors.append("decision_timestamp_not_strictly_increasing")
    return tuple(errors)


class ShadowObservationWriter:
    """Append-only JSONL writer with a durable, validated ordering boundary."""

    def __init__(self, output_path: str | Path):
        self.path = Path(output_path)
        if not str(self.path).strip():
            raise ValueError("output_path must be explicit")
        if self.path.exists() and self.path.is_dir():
            raise CaptureFileError("output_path is a directory")
        self.counters = CaptureCounters(output_path=str(self.path))
        self._last_row: dict[str, Any] | None = None
        self._load_existing_tail()

    @property
    def last_source_timestamp(self) -> float | None:
        return self.counters.last_source_timestamp

    @property
    def last_decision_timestamp(self) -> float | None:
        return self.counters.last_decision_timestamp

    def _load_existing_tail(self) -> None:
        if not self.path.exists():
            return
        previous_source = None
        previous_decision = None
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CaptureFileError(f"invalid JSONL at line {line_number}") from exc
                if not isinstance(row, Mapping):
                    raise CaptureFileError(f"JSONL line {line_number} is not an object")
                validation_errors = observation_validation_errors(row)
                if validation_errors:
                    joined = ", ".join(validation_errors)
                    raise CaptureFileError(f"invalid existing row at line {line_number}: {joined}")
                ordering_errors = _ordering_errors(row, previous_source, previous_decision)
                if ordering_errors:
                    raise CaptureFileError(
                        f"invalid existing ordering at line {line_number}: "
                        + ", ".join(ordering_errors)
                    )
                previous_source = _number(row["source_timestamp"])
                previous_decision = _number(row["decision_timestamp"])
                self._last_row = dict(row)
        self.counters.last_source_timestamp = previous_source
        self.counters.last_decision_timestamp = previous_decision

    def append(self, row: Mapping[str, Any]) -> str:
        validation_errors = observation_validation_errors(row)
        if validation_errors:
            self.counters.invalid_or_unready_skipped += 1
            self.counters.last_error = "INVALID_OBSERVATION:" + ",".join(validation_errors)
            return "invalid"
        ordering_errors = _ordering_errors(
            row, self.last_source_timestamp, self.last_decision_timestamp
        )
        if ordering_errors == ("duplicate_observation",):
            self.counters.duplicates_skipped += 1
            self.counters.last_error = None
            return "duplicate"
        if ordering_errors:
            self.counters.invalid_or_unready_skipped += 1
            self.counters.last_error = "OUT_OF_ORDER:" + ",".join(ordering_errors)
            return "out_of_order"

        record = dict(row)
        if record.get("native_order_id") in (None, ""):
            record.pop("native_order_id", None)
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            self.counters.invalid_or_unready_skipped += 1
            self.counters.last_error = f"SERIALIZATION:{type(exc).__name__}"
            return "invalid"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._last_row = record
        self.counters.records_written += 1
        self.counters.last_source_timestamp = _number(record["source_timestamp"])
        self.counters.last_decision_timestamp = _number(record["decision_timestamp"])
        self.counters.last_error = None
        return "written"

    def observability(self) -> dict[str, Any]:
        return self.counters.to_dict()


def _record_controller_context(counters: CaptureCounters, controller: Mapping[str, Any]) -> None:
    counters.last_market_state = _text(controller.get("market_state"))
    counters.last_grid_mode = _text(controller.get("grid_mode"))
    counters.last_iv_ratio = _number(_mapping(controller.get("iv_state")).get("iv_ratio"))
    runtime = _mapping(controller.get("runtime"))
    counters.last_controller_status = _text(runtime.get("controller_status"))
    perp = _mapping(controller.get("perp"))
    counters.last_bbo_marker = perp.get("order_book_marker")
    counters.last_bid = _number(perp.get("bid"))
    counters.last_ask = _number(perp.get("ask"))
    counters.last_mid = _number(perp.get("mid"))
    capital = _mapping(controller.get("capital"))
    counters.last_available_collateral = _number(capital.get("available_collateral_quote"))
    counters.last_deployable_capital = _number(capital.get("deployable_quote"))
    counters.last_target_capital = _number(capital.get("target_strategy_quote"))
    counters.last_fee_buffer = _number(capital.get("fee_buffer_quote"))
    counters.last_capital_ready = (
        bool(capital["capital_ready"]) if "capital_ready" in capital else None
    )
    counters.last_entry_safety_ready = (
        bool(_mapping(controller.get("entry_safety"))["semantics_ready"])
        if "semantics_ready" in _mapping(controller.get("entry_safety"))
        else None
    )
    counters.last_exit_safety_ready = (
        bool(_mapping(controller.get("exit_safety"))["semantics_ready"])
        if "semantics_ready" in _mapping(controller.get("exit_safety"))
        else None
    )
    counters.last_fee_economics_ready = (
        bool(_mapping(controller.get("fee_economics"))["ready"])
        if "ready" in _mapping(controller.get("fee_economics"))
        else None
    )
    fee_economics = _mapping(controller.get("fee_economics"))
    counters.last_fee_model_status = _text(fee_economics.get("fee_model_status"))
    counters.last_fee_source_mismatch = (
        bool(fee_economics["fee_source_mismatch"])
        if "fee_source_mismatch" in fee_economics
        else None
    )
    connector_proof = _mapping(controller.get("connector_proof"))
    counters.last_connector_proof_status = _text(connector_proof.get("status"))
    counters.last_connector_contract_verified = (
        bool(connector_proof["contract_verified"])
        if "contract_verified" in connector_proof
        else None
    )
    counters.last_connector_open_reduce_only = (
        bool(connector_proof["open_reduce_only"])
        if "open_reduce_only" in connector_proof
        else None
    )
    counters.last_connector_close_reduce_only = (
        bool(connector_proof["close_reduce_only"])
        if "close_reduce_only" in connector_proof
        else None
    )
    counters.last_connector_post_only = (
        bool(connector_proof["limit_maker_post_only"])
        if "limit_maker_post_only" in connector_proof
        else None
    )
    counters.last_connector_limit_tif = _text(connector_proof.get("limit_tif"))
    counters.last_connector_maker_tif = _text(connector_proof.get("limit_maker_tif"))
    counters.last_connector_lifecycle = _text(connector_proof.get("lifecycle"))
    lifecycle_evidence = _mapping(controller.get("lifecycle_evidence"))
    if not lifecycle_evidence:
        lifecycle_evidence = connector_proof
    counters.last_lifecycle_evidence_present = (
        bool(lifecycle_evidence["present"]) if "present" in lifecycle_evidence else None
    )
    counters.last_lifecycle_evidence_approved = (
        bool(lifecycle_evidence["approved"]) if "approved" in lifecycle_evidence else None
    )
    counters.last_lifecycle_evidence_id = _text(
        lifecycle_evidence.get("evidence_id", lifecycle_evidence.get("lifecycle_evidence_id"))
    )
    counters.last_lifecycle_evidence_sha256 = _text(
        lifecycle_evidence.get("sha256", lifecycle_evidence.get("lifecycle_evidence_sha256"))
    )
    counters.last_runtime_contract_id = _text(lifecycle_evidence.get("runtime_contract_id"))
    counters.last_runtime_instance_id = _text(lifecycle_evidence.get("runtime_instance_id"))
    counters.last_runtime_image_ref = _text(lifecycle_evidence.get("runtime_image_ref"))
    counters.last_runtime_manifest_verified = (
        bool(lifecycle_evidence["runtime_manifest_verified"])
        if "runtime_manifest_verified" in lifecycle_evidence
        else None
    )
    counters.last_lifecycle_evidence_blockers = [
        str(item) for item in lifecycle_evidence.get("blockers", ()) or ()
    ]
    readiness = _mapping(controller.get("live_readiness"))
    counters.last_live_readiness = _text(readiness.get("status"))
    account = _mapping(controller.get("account_cleanliness"))
    counters.last_account_fingerprint = _text(account.get("account_fingerprint"))
    counters.last_account_clean = bool(account["clean"]) if "clean" in account else None
    counters.last_execution_blockers = [
        str(item) for item in controller.get("execution_blockers", ()) or ()
    ]


def capture_status(payload: Any, writer: ShadowObservationWriter) -> str:
    """Process one bot-status payload and return the capture outcome."""

    controller = _find_sol(_unwrap(payload))
    if not controller:
        writer.counters.invalid_or_unready_skipped += 1
        writer.counters.last_error = "CONTROLLER_DIAGNOSTICS_MISSING"
        return "unready"
    _record_controller_context(writer.counters, controller)
    execution = _mapping(controller.get("execution"))
    runtime = _mapping(controller.get("runtime"))
    perp = _mapping(controller.get("perp"))
    grid_mode = str(controller.get("grid_mode", "UNKNOWN"))
    if not (
        execution.get("mainnet_armed") is False
        and execution.get("execution_enabled") is False
        and execution.get("manual_kill_switch") is False
        and str(runtime.get("controller_status", "")).upper() == "RUNNING"
    ):
        writer.counters.unsafe_gate_skipped += 1
        writer.counters.last_error = "UNSAFE_EXECUTION_GATES_OR_CONTROLLER_NOT_RUNNING"
        return "unsafe_gates"
    if grid_mode not in {"AGGRESSIVE", "NORMAL", "DEFENSIVE"}:
        writer.counters.invalid_or_unready_skipped += 1
        writer.counters.last_error = "UNKNOWN_GRID_MODE"
        return "unready"
    if controller.get("perp_market_ready") is not True or not perp.get("pricing_ready", False):
        writer.counters.invalid_or_unready_skipped += 1
        writer.counters.last_error = "PRICING_NOT_READY"
        return "unready"
    if controller.get("options_data_available") is not True:
        writer.counters.invalid_or_unready_skipped += 1
        writer.counters.last_error = "OPTIONS_NOT_READY"
        return "unready"
    if controller.get("calibration_observation_ready") is not True:
        writer.counters.invalid_or_unready_skipped += 1
        errors = controller.get("calibration_observation_errors") or ("not_ready",)
        writer.counters.last_error = "UNREADY:" + ",".join(str(error) for error in errors)
        return "unready"
    observation = controller.get("calibration_observation")
    if not isinstance(observation, Mapping):
        writer.counters.invalid_or_unready_skipped += 1
        writer.counters.last_error = "CALIBRATION_OBSERVATION_MISSING"
        return "unready"
    record = dict(observation)
    record["controller_metrics"] = {
        "market_state": controller.get("market_state"),
        "grid_mode": controller.get("grid_mode"),
        "iv_state": _mapping(controller.get("iv_state")),
        "inventory": _mapping(controller.get("inventory")),
        "capital": _mapping(controller.get("capital")),
        "entry_safety": _mapping(controller.get("entry_safety")),
        "exit_safety": _mapping(controller.get("exit_safety")),
        "connector_proof": _mapping(controller.get("connector_proof")),
        "fee_economics": _mapping(controller.get("fee_economics")),
        "account_cleanliness": _mapping(controller.get("account_cleanliness")),
        "risk_gates": _mapping(controller.get("risk_gates")),
        "live_readiness": _mapping(controller.get("live_readiness")),
        "execution_blockers": list(controller.get("execution_blockers", ()) or ()),
        "lifecycle_evidence": _mapping(controller.get("lifecycle_evidence")),
    }
    return writer.append(record)


def _report(writer: ShadowObservationWriter, report: Any, bot_name: str, ticks: int) -> None:
    report.clear()
    report.builder.manual_order()
    report.builder.kpi("Bot", bot_name)
    report.builder.kpi("Ticks", ticks)
    report.builder.kpi("Records written", writer.counters.records_written)
    report.builder.kpi("Duplicates skipped", writer.counters.duplicates_skipped)
    report.builder.kpi("Invalid/unready skipped", writer.counters.invalid_or_unready_skipped)
    report.builder.kpi("Unsafe gate skips", writer.counters.unsafe_gate_skipped)
    report.builder.kpi("Controller status", writer.counters.last_controller_status or "N/A")
    report.builder.kpi("Last market state", writer.counters.last_market_state or "N/A")
    report.builder.kpi("Last grid mode", writer.counters.last_grid_mode or "N/A")
    report.builder.kpi(
        "Last BBO bid / ask / mid",
        f"{writer.counters.last_bid} / {writer.counters.last_ask} / {writer.counters.last_mid}",
    )
    report.builder.kpi(
        "Last IV ratio",
        "N/A" if writer.counters.last_iv_ratio is None else writer.counters.last_iv_ratio,
    )
    report.builder.kpi(
        "Capital available / target / deployable",
        f"{writer.counters.last_available_collateral} / "
        f"{writer.counters.last_target_capital} / "
        f"{writer.counters.last_deployable_capital}",
    )
    report.builder.kpi(
        "Entry / exit / fee ready",
        f"{writer.counters.last_entry_safety_ready} / "
        f"{writer.counters.last_exit_safety_ready} / "
        f"{writer.counters.last_fee_economics_ready}",
    )
    report.builder.kpi(
        "Connector proof",
        f"{writer.counters.last_connector_proof_status or 'UNVERIFIED'} / "
        f"contract={writer.counters.last_connector_contract_verified} / "
        f"open={writer.counters.last_connector_open_reduce_only} / "
        f"close={writer.counters.last_connector_close_reduce_only} / "
        f"post_only={writer.counters.last_connector_post_only} / "
        f"limit_tif={writer.counters.last_connector_limit_tif or 'UNKNOWN'} / "
        f"maker_tif={writer.counters.last_connector_maker_tif or 'UNKNOWN'} / "
        f"lifecycle={writer.counters.last_connector_lifecycle or 'UNVERIFIED'}",
    )
    report.builder.kpi(
        "Lifecycle evidence",
        f"present={writer.counters.last_lifecycle_evidence_present} / "
        f"approved={writer.counters.last_lifecycle_evidence_approved} / "
        f"runtime={writer.counters.last_runtime_contract_id or 'UNKNOWN'} / "
        f"instance={writer.counters.last_runtime_instance_id or 'UNKNOWN'} / "
        f"image={writer.counters.last_runtime_image_ref or 'UNKNOWN'}",
    )
    report.builder.kpi(
        "Lifecycle blockers",
        ", ".join(writer.counters.last_lifecycle_evidence_blockers or ()) or "None",
    )
    report.builder.kpi(
        "Fee model",
        f"{writer.counters.last_fee_model_status or 'UNKNOWN'} / "
        f"source_mismatch={writer.counters.last_fee_source_mismatch}",
    )
    report.builder.kpi(
        "Account clean / live readiness",
        f"{writer.counters.last_account_clean} / {writer.counters.last_live_readiness}",
    )
    report.builder.kpi(
        "Account fingerprint",
        writer.counters.last_account_fingerprint or "UNKNOWN",
    )
    report.builder.table([writer.observability()])
    report.builder.markdown(
        "## Read-only boundary\n"
        "- `records_written` is a shadow-data count, not a real fill count.\n"
        "- Every persisted row must carry `evidence=SHADOW_PLAN`.\n"
        "- The routine only reads bot status and appends to the configured JSONL path."
    )


async def run(config: Config, context: Any) -> str:
    """Continuously capture causal observations without mutating trading state."""

    if config.execution_enabled:
        raise RuntimeError("derive_options_grid_shadow_capture refused execution_enabled=true")
    from config_manager import get_client

    from condor.reports import LiveReport

    writer = ShadowObservationWriter(config.output_path)
    chat_id = getattr(context, "_chat_id", None)
    report = LiveReport(
        "Derive SOL Options Adaptive Grid Shadow Capture",
        source_name="derive_options_grid_shadow_capture",
        tags=["derive", "sol", "options", "grid", "shadow", "read-only"],
        auto_refresh_seconds=config.report_auto_refresh_seconds,
    )
    ticks = 0
    try:
        while True:
            try:
                client = await get_client(chat_id, context=context)
                if client is None:
                    writer.counters.invalid_or_unready_skipped += 1
                    writer.counters.last_error = "HUMMINGBOT_CLIENT_UNAVAILABLE"
                else:
                    payload = await client.bot_orchestration.get_bot_status(config.bot_name)
                    capture_status(payload, writer)
                ticks += 1
                _report(writer, report, config.bot_name, ticks)
                await report.update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                writer.counters.last_error = f"TICK_ERROR:{type(exc).__name__}"
                ticks += 1
                _report(writer, report, config.bot_name, ticks)
                await report.update()
            await asyncio.sleep(config.poll_interval_seconds)
    except asyncio.CancelledError:
        raise


__all__ = [
    "ASSETS",
    "CATEGORY",
    "CONTINUOUS",
    "CaptureCounters",
    "CaptureFileError",
    "Config",
    "ShadowObservationWriter",
    "capture_status",
    "observation_validation_errors",
    "run",
]
