"""Canonical timestamped records used by the offline calibration layer."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

CALIBRATION_EVIDENCE_VALUES = frozenset(
    {
        "PUBLIC_MARKET_DATA",
        "SHADOW_PLAN",
        "PROXY_TOUCH",
        "SIMULATED_MID_TOUCH",
        "SIMULATED_BBO_CROSS",
        "SIMULATED_HIGH_LOW_TOUCH",
        "REAL_EXECUTOR_FILL",
    }
)


def _float(value: Any, *, field: str, required: bool = False) -> float | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    """One signal observation available at ``decision_timestamp``.

    The call and put IV fields are mandatory for an accepted signal because
    the live strategy requires a same-strike pair.  ``atm_iv`` is normalized
    from their mean when omitted; it is never carried forward from another
    row.
    """

    decision_timestamp: float
    source_timestamp: float | None = None
    received_timestamp: float | None = None
    underlying: str | None = None
    trading_pair: str | None = None
    exchange_instrument: str | None = None
    environment: str | None = None
    perp_mid: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    atm_call_iv: float | None = None
    atm_put_iv: float | None = None
    atm_iv: float | None = None
    expiry_timestamp: float | None = None
    days_to_expiry: float | None = None
    atm_strike: float | None = None
    call_strike: float | None = None
    put_strike: float | None = None
    call_instrument: str | None = None
    put_instrument: str | None = None
    iv_source: str | None = None
    call_iv_source: str | None = None
    put_iv_source: str | None = None
    option_reference_price: float | None = None
    native_order_id: str | None = None
    source: str | None = None
    evidence: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> CalibrationObservation:
        """Build a canonical row from JSON/CSV names used by the controller."""

        call_iv = _float(_first(row, "atm_call_iv", "call_iv"), field="atm_call_iv")
        put_iv = _float(_first(row, "atm_put_iv", "put_iv"), field="atm_put_iv")
        atm_iv = _float(_first(row, "atm_iv"), field="atm_iv")
        if atm_iv is None and call_iv is not None and put_iv is not None:
            atm_iv = (call_iv + put_iv) / 2.0
        decision_timestamp = _float(
            _first(row, "decision_timestamp", "decision_time"),
            field="decision_timestamp",
            required=True,
        )
        expiry_timestamp = _float(row.get("expiry_timestamp"), field="expiry_timestamp")
        days_to_expiry = _float(row.get("days_to_expiry"), field="days_to_expiry")
        if days_to_expiry is None and expiry_timestamp is not None:
            days_to_expiry = (expiry_timestamp - decision_timestamp) / (24.0 * 60.0 * 60.0)
        common_iv_source = _optional_text(row.get("iv_source"))
        call_iv_source = _optional_text(row.get("call_iv_source"))
        put_iv_source = _optional_text(row.get("put_iv_source"))
        display_iv_source = common_iv_source
        if display_iv_source is None and call_iv_source == put_iv_source:
            display_iv_source = call_iv_source
        return cls(
            decision_timestamp=decision_timestamp,
            source_timestamp=_float(
                _first(row, "source_timestamp", "iv_source_timestamp"),
                field="source_timestamp",
            ),
            received_timestamp=_float(
                _first(row, "received_timestamp", "receipt_timestamp"),
                field="received_timestamp",
            ),
            underlying=_optional_text(row.get("underlying")),
            trading_pair=_optional_text(row.get("trading_pair")),
            exchange_instrument=_optional_text(row.get("exchange_instrument")),
            environment=_optional_text(row.get("environment")),
            perp_mid=_float(_first(row, "perp_mid", "mid"), field="perp_mid"),
            best_bid=_float(_first(row, "best_bid", "bid"), field="best_bid"),
            best_ask=_float(_first(row, "best_ask", "ask"), field="best_ask"),
            atm_call_iv=call_iv,
            atm_put_iv=put_iv,
            atm_iv=atm_iv,
            expiry_timestamp=expiry_timestamp,
            days_to_expiry=days_to_expiry,
            atm_strike=_float(row.get("atm_strike"), field="atm_strike"),
            call_strike=_float(row.get("call_strike"), field="call_strike"),
            put_strike=_float(row.get("put_strike"), field="put_strike"),
            call_instrument=_optional_text(row.get("call_instrument")),
            put_instrument=_optional_text(row.get("put_instrument")),
            iv_source=display_iv_source,
            call_iv_source=call_iv_source,
            put_iv_source=put_iv_source,
            option_reference_price=_float(
                _first(row, "option_reference_price", "reference_price"),
                field="option_reference_price",
            ),
            native_order_id=_optional_text(
                _first(row, "native_order_id", "exchange_order_id", "order_id")
            ),
            source=_optional_text(row.get("source")),
            evidence=_optional_text(_first(row, "evidence", "evidence_kind")),
        )

    @property
    def resolved_atm_iv(self) -> float | None:
        if self.atm_call_iv is None or self.atm_put_iv is None:
            return None
        return (self.atm_call_iv + self.atm_put_iv) / 2.0

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.underlying is None:
            errors.append("underlying_missing")
        elif self.underlying.strip().upper() != "SOL":
            errors.append("underlying_not_SOL")
        if self.trading_pair is None:
            errors.append("trading_pair_missing")
        elif self.trading_pair.strip().upper() != "SOL-USDC":
            errors.append("trading_pair_not_SOL-USDC")
        if self.exchange_instrument is None:
            errors.append("exchange_instrument_missing")
        elif self.exchange_instrument.strip().upper() != "SOL-PERP":
            errors.append("exchange_instrument_not_SOL-PERP")
        if self.environment is None:
            errors.append("environment_missing")
        elif self.environment.strip().lower() != "mainnet":
            errors.append("environment_not_mainnet")
        if self.source is None:
            errors.append("source_missing")
        if self.evidence is None:
            errors.append("evidence_missing")
        if self.source_timestamp is None or self.received_timestamp is None:
            errors.append("causal_timestamps_missing")
        else:
            if self.source_timestamp > self.received_timestamp:
                errors.append("source_after_receipt")
            if self.received_timestamp > self.decision_timestamp:
                errors.append("receipt_after_decision")
            if self.source_timestamp > self.decision_timestamp:
                errors.append("source_after_decision")
        if self.atm_call_iv is None or self.atm_put_iv is None:
            errors.append("same_strike_call_put_iv_required")
        elif self.atm_call_iv <= 0 or self.atm_put_iv <= 0:
            errors.append("iv_must_be_positive")
        if self.atm_iv is not None and self.atm_iv <= 0:
            errors.append("atm_iv_must_be_positive")
        if self.expiry_timestamp is None and self.days_to_expiry is None:
            errors.append("expiry_or_dte_required")
        if self.expiry_timestamp is not None and self.expiry_timestamp <= self.decision_timestamp:
            errors.append("expiry_must_be_after_decision")
        if self.expiry_timestamp is not None and self.days_to_expiry is not None:
            derived_dte = (self.expiry_timestamp - self.decision_timestamp) / (24.0 * 60.0 * 60.0)
            if not math.isclose(self.days_to_expiry, derived_dte, rel_tol=1e-6, abs_tol=1e-6):
                errors.append("expiry_dte_mismatch")
        if self.days_to_expiry is not None and not 2.0 <= self.days_to_expiry <= 14.0:
            errors.append("days_to_expiry_outside_2_to_14")
        for name, strike in (
            ("atm_strike", self.atm_strike),
            ("call_strike", self.call_strike),
            ("put_strike", self.put_strike),
        ):
            if strike is None:
                errors.append(f"{name}_missing")
            elif strike <= 0:
                errors.append(f"{name}_must_be_positive")
        if (
            self.call_strike is not None
            and self.put_strike is not None
            and not math.isclose(self.call_strike, self.put_strike, rel_tol=1e-9, abs_tol=1e-9)
        ):
            errors.append("call_put_strike_mismatch")
        if (
            self.atm_strike is not None
            and self.call_strike is not None
            and not math.isclose(self.atm_strike, self.call_strike, rel_tol=1e-9, abs_tol=1e-9)
        ):
            errors.append("atm_call_strike_mismatch")
        if (
            self.atm_strike is not None
            and self.put_strike is not None
            and not math.isclose(self.atm_strike, self.put_strike, rel_tol=1e-9, abs_tol=1e-9)
        ):
            errors.append("atm_put_strike_mismatch")
        if self.call_instrument is None:
            errors.append("call_instrument_missing")
        if self.put_instrument is None:
            errors.append("put_instrument_missing")
        if self.call_iv_source is None:
            errors.append("call_iv_source_missing")
        if self.put_iv_source is None:
            errors.append("put_iv_source_missing")
        if self.evidence is not None:
            normalized_evidence = self.evidence.strip().upper()
            if normalized_evidence not in CALIBRATION_EVIDENCE_VALUES:
                errors.append("evidence_unknown")
            elif normalized_evidence == "REAL_EXECUTOR_FILL" and self.native_order_id is None:
                errors.append("native_order_id_required_for_real_fill")
        if self.option_reference_price is None:
            errors.append("option_reference_price_missing")
        elif self.option_reference_price <= 0:
            errors.append("option_reference_price_must_be_positive")
        elif self.atm_strike is not None:
            atm_distance = (
                abs(self.atm_strike - self.option_reference_price) / self.option_reference_price
            )
            if atm_distance > 0.05:
                errors.append("atm_distance_above_5pct")
        if self.perp_mid is not None and self.perp_mid <= 0:
            errors.append("perp_mid_must_be_positive")
        if self.best_bid is not None and self.best_bid <= 0:
            errors.append("best_bid_must_be_positive")
        if self.best_ask is not None and self.best_ask <= 0:
            errors.append("best_ask_must_be_positive")
        if (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_ask < self.best_bid
        ):
            errors.append("best_ask_below_best_bid")
        if self.atm_iv is not None and self.resolved_atm_iv is not None:
            if not math.isclose(self.atm_iv, self.resolved_atm_iv, rel_tol=1e-6, abs_tol=1e-9):
                errors.append("atm_iv_not_call_put_mean")
        return tuple(errors)

    def canonical_atm_iv(self) -> float:
        if self.resolved_atm_iv is None:
            raise ValueError("same-strike call and put IV are required")
        return self.resolved_atm_iv

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["atm_iv"] = self.canonical_atm_iv() if self.resolved_atm_iv is not None else None
        return row


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A future price observation used only to label an earlier feature."""

    timestamp: float
    mid: float
    best_bid: float | None = None
    best_ask: float | None = None
    high: float | None = None
    low: float | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> PricePoint:
        return cls(
            timestamp=_float(
                _first(row, "timestamp", "decision_timestamp"),
                field="timestamp",
                required=True,
            ),
            mid=_float(_first(row, "mid", "perp_mid"), field="mid", required=True),
            best_bid=_float(_first(row, "best_bid", "bid"), field="best_bid"),
            best_ask=_float(_first(row, "best_ask", "ask"), field="best_ask"),
            high=_float(row.get("high"), field="high"),
            low=_float(row.get("low"), field="low"),
        )
