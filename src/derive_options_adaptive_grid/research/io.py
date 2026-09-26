"""JSONL/CSV input and read-only shadow-data collection helpers."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CalibrationObservation

INCOMPATIBLE_REASONS = frozenset(
    {
        "underlying_missing",
        "underlying_not_SOL",
        "trading_pair_missing",
        "trading_pair_not_SOL-USDC",
        "exchange_instrument_missing",
        "exchange_instrument_not_SOL-PERP",
        "environment_missing",
        "environment_not_mainnet",
        "expiry_or_dte_required",
        "days_to_expiry_outside_2_to_14",
        "atm_strike_missing",
        "call_strike_missing",
        "put_strike_missing",
        "call_put_strike_mismatch",
        "atm_call_strike_mismatch",
        "atm_put_strike_mismatch",
        "call_instrument_missing",
        "put_instrument_missing",
        "call_iv_source_missing",
        "put_iv_source_missing",
        "option_reference_price_missing",
        "option_reference_price_must_be_positive",
        "atm_distance_above_5pct",
        "evidence_unknown",
        "native_order_id_required_for_real_fill",
        "source_missing",
        "evidence_missing",
    }
)


@dataclass(frozen=True, slots=True)
class ObservationAudit:
    source_path: str | None
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    incompatible_rows: int
    rejection_reasons: dict[str, int]
    first_decision_timestamp: float | None
    last_decision_timestamp: float | None
    evidence_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "incompatible_rows": self.incompatible_rows,
            "rejection_reasons": dict(self.rejection_reasons),
            "first_decision_timestamp": self.first_decision_timestamp,
            "last_decision_timestamp": self.last_decision_timestamp,
            "evidence_counts": dict(self.evidence_counts),
        }


def _rows_from_path(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    with path.open(encoding="utf-8") as handle:
        if path.suffix.lower() == ".json":
            payload = json.load(handle)
            if isinstance(payload, dict):
                payload = payload.get("observations", [])
            if not isinstance(payload, list):
                raise ValueError("JSON input must be a list or an observations object")
            return [row for row in payload if isinstance(row, dict)]
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            rows.append(row)
        return rows


def load_observations(path: str | Path) -> list[CalibrationObservation]:
    """Load rows without sorting or forward-filling them."""

    return [CalibrationObservation.from_mapping(row) for row in _rows_from_path(Path(path))]


def audit_observations(
    rows: Iterable[CalibrationObservation],
    *,
    source_path: str | None = None,
    max_source_age_seconds: float | None = 15.0,
) -> tuple[list[CalibrationObservation], ObservationAudit]:
    """Validate rows in input order and reject duplicates/out-of-order data."""

    accepted: list[CalibrationObservation] = []
    reasons: Counter[str] = Counter()
    evidence: Counter[str] = Counter()
    previous_decision: float | None = None
    previous_source: float | None = None
    total = 0
    incompatible_rows = 0
    for row in rows:
        total += 1
        row_errors = list(row.validation_errors())
        if (
            max_source_age_seconds is not None
            and row.source_timestamp is not None
            and row.decision_timestamp - row.source_timestamp > max_source_age_seconds
        ):
            row_errors.append("source_timestamp_stale")
        if previous_decision is not None and row.decision_timestamp <= previous_decision:
            row_errors.append("decision_timestamp_not_strictly_increasing")
        if (
            previous_source is not None
            and row.source_timestamp is not None
            and row.source_timestamp <= previous_source
        ):
            row_errors.append("source_timestamp_not_strictly_increasing")
        if row_errors:
            unique_errors = dict.fromkeys(row_errors)
            if INCOMPATIBLE_REASONS.intersection(unique_errors):
                incompatible_rows += 1
                reasons["INCOMPATIBLE_FOR_CALIBRATION"] += 1
            for reason in unique_errors:
                reasons[reason] += 1
            continue
        accepted.append(row)
        evidence[row.evidence.strip().upper()] += 1
        previous_decision = row.decision_timestamp
        previous_source = row.source_timestamp
    audit = ObservationAudit(
        source_path=source_path,
        total_rows=total,
        accepted_rows=len(accepted),
        rejected_rows=total - len(accepted),
        incompatible_rows=incompatible_rows,
        rejection_reasons=dict(reasons),
        first_decision_timestamp=accepted[0].decision_timestamp if accepted else None,
        last_decision_timestamp=accepted[-1].decision_timestamp if accepted else None,
        evidence_counts=dict(evidence),
    )
    return accepted, audit


def append_shadow_observation(path: str | Path, observation: CalibrationObservation) -> None:
    """Append one already-normalized, read-only shadow observation as JSONL."""

    errors = observation.validation_errors()
    if errors:
        raise ValueError("invalid shadow observation: " + ", ".join(errors))
    path = Path(path)
    if path.exists():
        existing = load_observations(path)
        _, audit = audit_observations(
            [*existing, observation],
            source_path=str(path),
            max_source_age_seconds=None,
        )
        if audit.rejected_rows:
            raise ValueError("shadow observation is duplicate or out of order")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(observation.to_dict(), sort_keys=True) + "\n")
