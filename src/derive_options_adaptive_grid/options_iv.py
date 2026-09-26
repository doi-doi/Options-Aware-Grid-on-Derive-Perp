"""Read-only Derive public SOL option-chain and IV adapter.

Only ``public/get_instruments`` and ``public/get_tickers`` are allowed.  Every
usable observation carries source and receipt timestamps so a controller can
reject stale, future-dated, or out-of-order data instead of forwarding the
last known IV.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import OptionsSnapshot

DAY_SECONDS = 86_400.0
DEFAULT_BASE_URL = "https://api.lyra.finance"
DEFAULT_SOURCE = "derive_public_get_tickers"
ALLOWED_METHODS = frozenset({"public/get_instruments", "public/get_tickers"})


class OptionsDataError(RuntimeError):
    """Raised when Derive cannot produce a usable public option observation."""


def _finite_float(value: Any) -> float | None:
    if value in (None, "", "null", "NaN", "nan"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp_seconds(value: Any) -> float | None:
    result = _finite_float(value)
    if result is None:
        return None
    return result / 1_000.0 if result > 10_000_000_000 else result


def _iso_utc(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _option_type(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return {"CALL": "C", "PUT": "P"}.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class OptionContract:
    instrument_name: str
    underlying: str
    expiry_timestamp: float
    strike: float
    option_type: str

    @property
    def expiry_label(self) -> str:
        return datetime.fromtimestamp(self.expiry_timestamp, UTC).strftime("%Y-%m-%d")

    @property
    def expiry_date(self) -> str:
        return datetime.fromtimestamp(self.expiry_timestamp, UTC).strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class ATMSelection:
    expiry_timestamp: float
    atm_strike: float
    atm_distance_pct: float
    call: OptionContract | None
    put: OptionContract | None


@dataclass(frozen=True, slots=True)
class OptionTicker:
    instrument_name: str
    option_type: str
    expiry_timestamp: float
    strike: float
    iv: float | None
    iv_source: str | None
    bid_iv: float | None
    ask_iv: float | None
    source_timestamp: float | None
    received_timestamp: float
    errors: tuple[str, ...] = ()


def parse_option_contract(row: dict[str, Any], *, underlying: str = "SOL") -> OptionContract | None:
    """Parse one active option metadata row without accepting non-options."""

    if not isinstance(row, dict):
        return None
    if str(row.get("instrument_type", "option")).lower() != "option":
        return None
    details = row.get("option_details") or row.get("optionDetails") or {}
    if not isinstance(details, dict):
        return None
    instrument_name = row.get("instrument_name") or row.get("instrumentName")
    expected_underlying = underlying.upper()
    identity_values = [
        row.get("currency"),
        row.get("base_currency"),
        row.get("baseCurrency"),
        row.get("underlying"),
        details.get("currency"),
        details.get("underlying"),
    ]
    explicit_identity = [
        str(value).strip().upper() for value in identity_values if value not in (None, "")
    ]
    if explicit_identity and any(value != expected_underlying for value in explicit_identity):
        return None
    if not explicit_identity and expected_underlying not in str(instrument_name or "").upper():
        return None
    expiry = _timestamp_seconds(details.get("expiry"))
    strike = _finite_float(details.get("strike"))
    option_type = _option_type(details.get("option_type", details.get("optionType")))
    if not instrument_name or expiry is None or strike is None or strike <= 0:
        return None
    if option_type not in {"C", "P"}:
        return None
    return OptionContract(str(instrument_name), expected_underlying, expiry, strike, option_type)


def parse_active_option_contracts(
    rows: Any,
    *,
    underlying: str = "SOL",
    now: float,
) -> list[OptionContract]:
    contracts: list[OptionContract] = []
    if not isinstance(rows, list):
        return contracts
    for row in rows:
        if not isinstance(row, dict) or row.get("is_active") is not True:
            continue
        contract = parse_option_contract(row, underlying=underlying)
        if contract and contract.expiry_timestamp > now:
            contracts.append(contract)
    return sorted(
        contracts,
        key=lambda item: (
            item.expiry_timestamp,
            item.strike,
            item.option_type,
            item.instrument_name,
        ),
    )


def select_expiry(
    contracts: list[OptionContract],
    *,
    now: float,
    min_days_to_expiry: float,
    target_days_to_expiry: float,
    max_days_to_expiry: float,
) -> tuple[float, list[OptionContract]]:
    if not 0 <= min_days_to_expiry <= target_days_to_expiry <= max_days_to_expiry:
        raise ValueError("DTE must satisfy 0 <= min <= target <= max")
    grouped: dict[float, list[OptionContract]] = {}
    for contract in contracts:
        dte = (contract.expiry_timestamp - now) / DAY_SECONDS
        if min_days_to_expiry <= dte <= max_days_to_expiry:
            grouped.setdefault(contract.expiry_timestamp, []).append(contract)
    eligible = [
        (expiry, rows)
        for expiry, rows in grouped.items()
        if any(item.option_type == "C" for item in rows)
        and any(item.option_type == "P" for item in rows)
    ]
    if not eligible:
        raise OptionsDataError("no active SOL expiry with both calls and puts in the DTE range")
    return min(
        eligible,
        key=lambda item: (abs((item[0] - now) / DAY_SECONDS - target_days_to_expiry), item[0]),
    )


def select_atm_strike(
    contracts: list[OptionContract],
    *,
    expiry_timestamp: float,
    reference_price: float,
    max_atm_distance_pct: float,
) -> ATMSelection:
    reference = _finite_float(reference_price)
    if reference is None or reference <= 0:
        raise OptionsDataError("SOL perpetual reference price is unavailable")
    if max_atm_distance_pct <= 0:
        raise ValueError("max_atm_distance_pct must be positive")
    rows = [item for item in contracts if item.expiry_timestamp == expiry_timestamp]
    if not rows:
        raise OptionsDataError("selected expiry has no option contracts")
    paired_strikes = {
        item.strike
        for item in rows
        if any(other.strike == item.strike and other.option_type == "C" for other in rows)
        and any(other.strike == item.strike and other.option_type == "P" for other in rows)
    }
    if not paired_strikes:
        raise OptionsDataError("selected expiry has no same-strike SOL call/put pair")
    strike = min(paired_strikes, key=lambda value: (abs(value - reference), value))
    distance = abs(strike - reference) / reference
    if distance > max_atm_distance_pct:
        raise OptionsDataError(f"nearest SOL ATM strike is {distance:.2%} from reference")
    same_strike = [item for item in rows if item.strike == strike]
    return ATMSelection(
        expiry_timestamp=expiry_timestamp,
        atm_strike=strike,
        atm_distance_pct=distance,
        call=next((item for item in same_strike if item.option_type == "C"), None),
        put=next((item for item in same_strike if item.option_type == "P"), None),
    )


def _read_iv(pricing: dict[str, Any], *keys: str, max_iv: float) -> float | None:
    for key in keys:
        value = _finite_float(pricing.get(key))
        if value is not None and 0 < value <= max_iv:
            return value
    return None


def parse_option_ticker(
    payload: Any,
    contract: OptionContract,
    *,
    received_timestamp: float,
    max_iv: float,
) -> OptionTicker:
    row = payload if isinstance(payload, dict) else {}
    pricing = row.get("option_pricing") or row.get("optionPricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}
    mark_iv = _read_iv(pricing, "i", "iv", "mark_iv", max_iv=max_iv)
    bid_iv = _read_iv(pricing, "bi", "bid_iv", max_iv=max_iv)
    ask_iv = _read_iv(pricing, "ai", "ask_iv", max_iv=max_iv)
    if mark_iv is not None:
        iv, source = mark_iv, "mark_iv"
    elif bid_iv is not None and ask_iv is not None:
        iv, source = (bid_iv + ask_iv) / 2.0, "bid_ask_iv_midpoint"
    elif bid_iv is not None:
        iv, source = bid_iv, "bid_iv_only"
    elif ask_iv is not None:
        iv, source = ask_iv, "ask_iv_only"
    else:
        iv, source = None, None
    source_timestamp = _timestamp_seconds(row.get("t", row.get("timestamp")))
    errors = () if iv is not None else ("no valid mark, bid, or ask IV",)
    if source_timestamp is None:
        errors += ("option ticker timestamp is unavailable",)
    return OptionTicker(
        instrument_name=contract.instrument_name,
        option_type=contract.option_type,
        expiry_timestamp=contract.expiry_timestamp,
        strike=contract.strike,
        iv=iv,
        iv_source=source,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        source_timestamp=source_timestamp,
        received_timestamp=received_timestamp,
        errors=errors,
    )


def _dedupe(errors: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(errors))


def build_options_snapshot(
    selection: ATMSelection,
    ticker_map: dict[str, Any],
    *,
    reference_price: float,
    now: float,
    received_timestamp: float | None = None,
    chain_contract_count: int = 0,
    max_option_data_age_seconds: float = 15.0,
    max_iv: float = 10.0,
    max_atm_distance_pct: float = 0.05,
    future_tolerance_seconds: float = 2.0,
    source: str = DEFAULT_SOURCE,
    environment: str = "mainnet",
) -> OptionsSnapshot:
    """Join the selected call/put and reject non-causal ticker observations."""

    received = now if received_timestamp is None else received_timestamp
    errors: list[str] = []
    valid: list[OptionTicker] = []
    for contract in (selection.call, selection.put):
        if contract is None:
            continue
        payload = ticker_map.get(contract.instrument_name)
        if payload is None:
            errors.append(f"ticker missing for {contract.instrument_name}")
            continue
        ticker = parse_option_ticker(
            payload,
            contract,
            received_timestamp=received,
            max_iv=max_iv,
        )
        errors.extend(ticker.errors)
        if ticker.source_timestamp is None:
            continue
        if ticker.source_timestamp > received + future_tolerance_seconds:
            errors.append(f"future ticker timestamp for {contract.instrument_name}")
            continue
        if ticker.source_timestamp > now + future_tolerance_seconds:
            errors.append(f"future decision timestamp for {contract.instrument_name}")
            continue
        if received > now + future_tolerance_seconds:
            errors.append("receipt timestamp is in the future")
            continue
        age = now - ticker.source_timestamp
        if age < -future_tolerance_seconds:
            continue
        if age > max_option_data_age_seconds:
            errors.append(
                f"ticker stale for {contract.instrument_name} ({age:.1f}s; "
                f"limit {max_option_data_age_seconds:.1f}s)"
            )
            continue
        if ticker.iv is not None:
            valid.append(ticker)
    call = next((item for item in valid if item.option_type == "C"), None)
    put = next((item for item in valid if item.option_type == "P"), None)
    iv_values = [item.iv for item in (call, put) if item and item.iv is not None]
    atm_iv = sum(iv_values) / len(iv_values) if iv_values else None
    timestamps = [item.source_timestamp for item in (call, put) if item and item.source_timestamp]
    source_timestamp = max(timestamps) if timestamps else None
    receipt_timestamps = [item.received_timestamp for item in (call, put) if item]
    actual_received = max(receipt_timestamps) if receipt_timestamps else received
    if call is None or put is None:
        errors.append("both ATM SOL call and put IV are required")
        confidence = 0.0
        available = False
    elif atm_iv is None:
        errors.append("ATM SOL IV unavailable after ticker validation")
        confidence = 0.0
        available = False
    else:
        side_factor = 1.0 if call and put else 0.75
        distance_factor = max(
            0.75,
            1.0 - 0.25 * min(1.0, selection.atm_distance_pct / max(1e-12, max_atm_distance_pct)),
        )
        confidence = round(side_factor * distance_factor, 3)
        available = True
    return OptionsSnapshot(
        underlying="SOL",
        reference_price=_finite_float(reference_price),
        expiry_timestamp=selection.expiry_timestamp,
        expiry=datetime.fromtimestamp(selection.expiry_timestamp, UTC).strftime("%Y-%m-%d"),
        days_to_expiry=max(0.0, (selection.expiry_timestamp - now) / DAY_SECONDS),
        atm_strike=selection.atm_strike,
        atm_distance_pct=selection.atm_distance_pct,
        call_instrument=selection.call.instrument_name if selection.call else None,
        put_instrument=selection.put.instrument_name if selection.put else None,
        call_iv=call.iv if call else None,
        put_iv=put.iv if put else None,
        atm_iv=atm_iv,
        call_iv_source=call.iv_source if call else None,
        put_iv_source=put.iv_source if put else None,
        source_timestamp=source_timestamp,
        received_timestamp=actual_received,
        decision_timestamp=now,
        source=source,
        environment=environment,
        data_available=available,
        confidence=confidence,
        chain_contract_count=chain_contract_count,
        ticker_count=len(ticker_map),
        valid_ticker_count=len(valid),
        errors=_dedupe(errors),
        call_strike=call.strike if call else None,
        put_strike=put.strike if put else None,
    )


def unavailable_options_snapshot(
    *,
    now: float,
    reference_price: float | None,
    errors: tuple[str, ...] | list[str],
    source: str = DEFAULT_SOURCE,
    environment: str = "mainnet",
) -> OptionsSnapshot:
    return OptionsSnapshot(
        underlying="SOL",
        reference_price=_finite_float(reference_price),
        expiry_timestamp=None,
        expiry=None,
        days_to_expiry=None,
        atm_strike=None,
        atm_distance_pct=None,
        call_instrument=None,
        put_instrument=None,
        call_iv=None,
        put_iv=None,
        atm_iv=None,
        call_iv_source=None,
        put_iv_source=None,
        source_timestamp=None,
        received_timestamp=None,
        decision_timestamp=now,
        source=source,
        environment=environment,
        data_available=False,
        confidence=0.0,
        chain_contract_count=0,
        ticker_count=0,
        valid_ticker_count=0,
        errors=_dedupe([str(error) for error in errors]),
        call_strike=None,
        put_strike=None,
    )


@dataclass
class DeriveOptionsProvider:
    """Bounded asynchronous wrapper around the two public Derive endpoints."""

    base_url: str = DEFAULT_BASE_URL
    currency: str = "SOL"
    environment: str = "mainnet"
    min_days_to_expiry: float = 2.0
    target_days_to_expiry: float = 7.0
    max_days_to_expiry: float = 14.0
    max_atm_distance_pct: float = 0.05
    max_option_data_age_seconds: float = 15.0
    future_tolerance_seconds: float = 2.0
    metadata_refresh_interval_seconds: float = 900.0
    request_timeout_seconds: float = 10.0
    max_iv: float = 10.0
    _contracts: list[OptionContract] = field(default_factory=list, init=False, repr=False)
    _metadata_fetched_at: float | None = field(default=None, init=False, repr=False)
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if self.currency.upper() != "SOL":
            raise ValueError("DeriveOptionsProvider is SOL-only")
        self.currency = "SOL"

    def _post(self, method: str, params: dict[str, Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"method is not allowed: {method}")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "derive-options-adaptive-grid/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                payload = json.load(response)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise OptionsDataError(f"{method} request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise OptionsDataError(f"{method} returned an unusable response")
        return payload["result"]

    def _get_contracts(self, now: float) -> list[OptionContract]:
        if (
            self._metadata_fetched_at is not None
            and now - self._metadata_fetched_at < self.metadata_refresh_interval_seconds
        ):
            return [item for item in self._contracts if item.expiry_timestamp > now]
        result = self._post(
            "public/get_instruments",
            {"currency": self.currency, "instrument_type": "option", "expired": False},
        )
        contracts = parse_active_option_contracts(result, underlying=self.currency, now=now)
        if not contracts:
            raise OptionsDataError("Derive returned no active future SOL option contracts")
        self._contracts = contracts
        self._metadata_fetched_at = now
        return contracts

    def _snapshot_sync(self, reference_price: float, request_timestamp: float) -> OptionsSnapshot:
        contracts = self._get_contracts(request_timestamp)
        expiry_timestamp, expiry_contracts = select_expiry(
            contracts,
            now=request_timestamp,
            min_days_to_expiry=self.min_days_to_expiry,
            target_days_to_expiry=self.target_days_to_expiry,
            max_days_to_expiry=self.max_days_to_expiry,
        )
        selection = select_atm_strike(
            expiry_contracts,
            expiry_timestamp=expiry_timestamp,
            reference_price=reference_price,
            max_atm_distance_pct=self.max_atm_distance_pct,
        )
        result = self._post(
            "public/get_tickers",
            {
                "currency": self.currency,
                "expiry_date": datetime.fromtimestamp(expiry_timestamp, UTC).strftime("%Y%m%d"),
                "instrument_type": "option",
            },
        )
        ticker_map = result.get("tickers") if isinstance(result, dict) else None
        if not isinstance(ticker_map, dict):
            raise OptionsDataError("Derive ticker response has no tickers map")
        received_timestamp = self.clock()
        decision_timestamp = max(self.clock(), received_timestamp)
        return build_options_snapshot(
            selection,
            ticker_map,
            reference_price=reference_price,
            now=decision_timestamp,
            received_timestamp=received_timestamp,
            chain_contract_count=len(contracts),
            max_option_data_age_seconds=self.max_option_data_age_seconds,
            max_iv=self.max_iv,
            max_atm_distance_pct=self.max_atm_distance_pct,
            future_tolerance_seconds=self.future_tolerance_seconds,
            environment=self.environment,
        )

    async def snapshot(
        self, reference_price: float | None, *, now: float | None = None
    ) -> OptionsSnapshot:
        decision_time = self.clock() if now is None else now
        reference = _finite_float(reference_price)
        if reference is None or reference <= 0:
            return unavailable_options_snapshot(
                now=decision_time,
                reference_price=reference,
                errors=("SOL perpetual reference price unavailable",),
                environment=self.environment,
            )
        try:
            return await asyncio.to_thread(self._snapshot_sync, reference, decision_time)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return unavailable_options_snapshot(
                now=decision_time,
                reference_price=reference,
                errors=(str(exc) or type(exc).__name__,),
                environment=self.environment,
            )


__all__ = [
    "ALLOWED_METHODS",
    "ATMSelection",
    "DAY_SECONDS",
    "DEFAULT_BASE_URL",
    "DeriveOptionsProvider",
    "OptionContract",
    "OptionTicker",
    "OptionsDataError",
    "build_options_snapshot",
    "parse_active_option_contracts",
    "parse_option_contract",
    "parse_option_ticker",
    "select_atm_strike",
    "select_expiry",
    "unavailable_options_snapshot",
]
