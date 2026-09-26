"""Read-only Derive SOL options IV monitor.

The options signal is public mainnet data from api.lyra.finance.  The SOL
reference is the Hummingbot derive_perpetual order-book BBO plus its tracker
marker.  No executor, order, position, leverage, or account mutation is
reachable from this module.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import statistics
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

NAME = "derive_options_grid_options_monitor"
DISPLAY_NAME = "Derive SOL Options IV Monitor"
CATEGORY = "Monitoring"
CONTINUOUS = True
ASSETS = ("SOL",)

PUBLIC_API_BASE = "https://api.lyra.finance"
PERPETUAL_CONNECTOR = "derive_perpetual"
PERPETUAL_PAIR_CANDIDATES = ("SOL-USDC", "SOL-PERP")
OPTION_MAX_AGE_SECONDS = 15.0
PERPETUAL_MAX_AGE_SECONDS = 10.0
FUTURE_SOURCE_TOLERANCE_SECONDS = 2.0
ATM_MAX_DISTANCE = 0.05
TARGET_DTE = 7.0
MIN_HISTORY = 5
MAX_HISTORY = 120

STATES = ("INITIALIZING", "LOW", "NORMAL", "HIGH", "EXTREME")
MODES = ("NOT_READY", "AGGRESSIVE", "NORMAL", "DEFENSIVE")

# The strategy adapter is consulted first when its installed modules expose a
# decision function.  The policy below is the monitor's versioned public
# contract and is only used when the strategy package is not available in the
# routine runtime.
STATE_POLICY = {
    "low_enter": 0.90,
    "low_exit": 0.95,
    "normal_high_enter": 1.10,
    "normal_high_exit": 1.05,
    "high_extreme_enter": 1.25,
    "high_extreme_exit": 1.20,
}

GRID_CONTEXTS = {
    "AGGRESSIVE": {
        "half_width_pct": 0.75,
        "full_width_pct": 1.50,
        "levels": 7,
        "nominal_gap_pct": 0.25,
        "nominal_gap_label": "NOMINAL",
        "order_quote_usd": 100.0,
    },
    "NORMAL": {
        "half_width_pct": 1.00,
        "full_width_pct": 2.00,
        "levels": 5,
        "nominal_gap_pct": 0.50,
        "nominal_gap_label": "NOMINAL",
        "order_quote_usd": 100.0,
    },
    "DEFENSIVE": {
        "half_width_pct": 2.50,
        "full_width_pct": 5.00,
        "levels": 3,
        "nominal_gap_pct": 2.50,
        "nominal_gap_label": "NOMINAL",
        "order_quote_usd": 60.0,
    },
}
GRID_COMMON = {
    "take_profit_pct": 0.10,
    "stop_loss": None,
    "time_limit": None,
    "executor": "GridExecutor",
    "entry_order_type": "LIMIT_MAKER",
    "take_profit_order_type": "LIMIT_MAKER",
}

STRATEGY_MODULE_CANDIDATES = (
    "options_iv",
    "state",
    "modes",
    "models",
    "hummingbot.strategy.market_making.derive_options_adaptive_grid.options_iv",
    "hummingbot.strategy.market_making.derive_options_adaptive_grid.state",
    "hummingbot.strategy.market_making.derive_options_adaptive_grid.modes",
    "hummingbot.strategy.market_making.derive_options_adaptive_grid.models",
)


class Config(BaseModel):
    """Read-only public Derive SOL options IV monitor with a live report."""

    refresh_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    metadata_cache_seconds: float = Field(default=900.0, ge=60.0, le=3600.0)
    report_auto_refresh_seconds: int = Field(default=5, ge=1, le=60)
    perpetual_connector: str = Field(default=PERPETUAL_CONNECTOR)
    perpetual_pair: str = Field(default="SOL-USDC")
    max_history: int = Field(default=MAX_HISTORY, ge=MIN_HISTORY, le=MAX_HISTORY)
    execution_enabled: bool = False
    allow_mainnet_trading: bool = False
    mainnet_armed: bool = False
    run_once: bool = False
    test_mode: bool = False

    @model_validator(mode="after")
    def enforce_read_only(self) -> Config:
        if self.execution_enabled:
            raise ValueError(f"{NAME} is read-only: execution_enabled must be false")
        if self.allow_mainnet_trading:
            raise ValueError(f"{NAME} is read-only: allow_mainnet_trading must be false")
        if self.mainnet_armed:
            raise ValueError(f"{NAME} is read-only: mainnet_armed must be false")
        if self.perpetual_connector != PERPETUAL_CONNECTOR:
            raise ValueError(f"{NAME} only supports {PERPETUAL_CONNECTOR}")
        return self


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    parsed = _number(value)
    if parsed is not None:
        if parsed > 100_000_000_000:
            parsed /= 1000.0
        return parsed
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()
        except ValueError:
            return None
    return None


def _iso(value: Any) -> str | None:
    epoch = _epoch(value)
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _path(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _valid_iv(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and 0.0 < parsed <= 10.0 else None


def _price_level(level: Any) -> float | None:
    if isinstance(level, Mapping):
        return _number(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _number(level[0])
    return None


def _text(value: Any, default: str = "N/A") -> str:
    return default if value is None else str(value)


def _pct(value: Any, digits: int = 2) -> str:
    parsed = _number(value)
    return "N/A" if parsed is None else f"{parsed:.{digits}f}%"


def _age(value: Any) -> str:
    parsed = _number(value)
    return "N/A" if parsed is None else f"{parsed:.2f}s"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if item))


def _load_strategy_adapter() -> Any | None:
    """Find the existing strategy policy if the Hummingbot image exposes it."""

    for module_name in STRATEGY_MODULE_CANDIDATES:
        try:
            return importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
    return None


def _strategy_decision(
    adapter: Any | None,
    ratio: float | None,
    previous_state: str,
) -> tuple[str, str] | None:
    """Reuse an installed strategy decision function without copying it."""

    if adapter is None or ratio is None:
        return None
    for name in ("decide_state", "classify_state", "hysteresis_state"):
        function = getattr(adapter, name, None)
        if not callable(function):
            continue
        for args in ((ratio, previous_state), (ratio,)):
            try:
                candidate = function(*args)
            except (TypeError, ValueError, KeyError):
                continue
            if isinstance(candidate, Mapping):
                state = str(candidate.get("state", "")).upper()
                mode = str(candidate.get("mode", "")).upper()
            elif isinstance(candidate, (list, tuple)) and len(candidate) >= 2:
                state, mode = str(candidate[0]).upper(), str(candidate[1]).upper()
            else:
                state = str(candidate).upper()
                mode = ""
            if state in STATES[1:]:
                if mode not in MODES:
                    mode = {
                        "LOW": "AGGRESSIVE",
                        "NORMAL": "NORMAL",
                        "HIGH": "DEFENSIVE",
                        "EXTREME": "DEFENSIVE",
                    }[state]
                return state, mode
    return None


def _initial_state(ratio: float) -> str:
    if ratio < STATE_POLICY["low_enter"]:
        return "LOW"
    if ratio <= STATE_POLICY["normal_high_enter"]:
        return "NORMAL"
    if ratio < STATE_POLICY["high_extreme_enter"]:
        return "HIGH"
    return "EXTREME"


def _hysteresis_state(
    ratio: float | None,
    previous_state: str,
    history_ready: bool,
    adapter: Any | None = None,
) -> tuple[str, str]:
    if ratio is None or not history_ready:
        return "INITIALIZING", "NOT_READY"

    reused = _strategy_decision(adapter, ratio, previous_state)
    if reused is not None:
        return reused

    previous = previous_state if previous_state in STATES else "INITIALIZING"
    if previous == "INITIALIZING":
        state = _initial_state(ratio)
    elif previous == "LOW":
        if ratio < STATE_POLICY["low_exit"]:
            state = "LOW"
        elif ratio <= STATE_POLICY["normal_high_enter"]:
            state = "NORMAL"
        elif ratio < STATE_POLICY["high_extreme_enter"]:
            state = "HIGH"
        else:
            state = "EXTREME"
    elif previous == "NORMAL":
        if ratio < STATE_POLICY["low_enter"]:
            state = "LOW"
        elif ratio > STATE_POLICY["normal_high_enter"]:
            state = "HIGH" if ratio < STATE_POLICY["high_extreme_enter"] else "EXTREME"
        else:
            state = "NORMAL"
    elif previous == "HIGH":
        if ratio >= STATE_POLICY["high_extreme_enter"]:
            state = "EXTREME"
        elif ratio <= STATE_POLICY["normal_high_exit"]:
            state = "NORMAL"
        else:
            state = "HIGH"
    else:  # EXTREME
        state = "HIGH" if ratio <= STATE_POLICY["high_extreme_exit"] else "EXTREME"

    mode = {
        "LOW": "AGGRESSIVE",
        "NORMAL": "NORMAL",
        "HIGH": "DEFENSIVE",
        "EXTREME": "DEFENSIVE",
    }[state]
    return state, mode


def _sanitize_history(raw: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for item in raw:
        row = _mapping(item)
        source_timestamp = _epoch(row.get("source_timestamp"))
        paired_iv = _valid_iv(row.get("paired_atm_iv"))
        if source_timestamp is None or paired_iv is None or source_timestamp in seen:
            continue
        seen.add(source_timestamp)
        rows.append(
            {
                "source_timestamp": source_timestamp,
                "paired_atm_iv": paired_iv,
                "expiry": row.get("expiry"),
                "strike": _number(row.get("strike")),
                "call_contract": row.get("call_contract"),
                "put_contract": row.get("put_contract"),
                "call_source_timestamp": _epoch(row.get("call_source_timestamp")),
                "put_source_timestamp": _epoch(row.get("put_source_timestamp")),
            }
        )
    rows.sort(key=lambda row: float(row["source_timestamp"]))
    return rows[-limit:]


def _state_slot(context: Any) -> dict[str, Any] | None:
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return None
    root = user_data.setdefault("_condor_routine_state", {})
    if not isinstance(root, dict):
        return None
    slot = root.setdefault(NAME, {})
    return slot if isinstance(slot, dict) else None


def _load_history(context: Any, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    slot = _state_slot(context)
    if slot is None:
        return [], "STATE_STORAGE_UNAVAILABLE"
    return _sanitize_history(slot.get("iv_history"), limit), None


def _save_history(
    context: Any,
    history: list[dict[str, Any]],
    limit: int,
) -> str | None:
    slot = _state_slot(context)
    if slot is None:
        return "STATE_STORAGE_UNAVAILABLE"
    # Only public, bounded IV observations are persisted.
    slot["iv_history"] = _sanitize_history(history, limit)
    return None


def _load_persisted_state(context: Any) -> str:
    slot = _state_slot(context)
    if slot is None:
        return "INITIALIZING"
    candidate = str(slot.get("state", "INITIALIZING")).upper()
    return candidate if candidate in STATES else "INITIALIZING"


def _persist_current_state(
    context: Any,
    snapshot: Mapping[str, Any],
    *,
    monitor_status: str,
    tick_count: int,
    report_id: str | None = None,
) -> str | None:
    """Persist only bounded, public machine state for the strategy adapter."""

    slot = _state_slot(context)
    if slot is None:
        return "STATE_STORAGE_UNAVAILABLE"
    signal = _mapping(snapshot.get("signal"))
    readiness = _mapping(snapshot.get("readiness"))
    state = str(snapshot.get("state") or signal.get("state") or "INITIALIZING")
    mode = str(snapshot.get("mode") or signal.get("mode") or "NOT_READY")
    signal_ready = bool(snapshot.get("signal_ready", readiness.get("signal_ready")))
    blockers = snapshot.get("blockers", [])
    if not isinstance(blockers, list):
        blockers = [blockers]
    history = _mapping(snapshot.get("history"))
    current = {
        "updated_at": snapshot.get("as_of"),
        "monitor_status": monitor_status,
        "tick_count": tick_count,
        "report_id": report_id,
        "signal_ready": signal_ready,
        "state": state,
        "mode": mode,
        "history_count": history.get("count", 0),
        "blockers": [str(item) for item in blockers],
        "read_only": True,
    }
    slot["current"] = current
    # Also expose the scalar fields directly for the adaptive-grid strategy.
    slot.update(
        {
            "signal_ready": signal_ready,
            "state": state,
            "mode": mode,
            "monitor_status": monitor_status,
            "tick_count": tick_count,
            "report_id": report_id,
            "updated_at": snapshot.get("as_of"),
        }
    )
    return None


def _update_history(
    history: list[dict[str, Any]],
    observation: dict[str, Any],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    timestamp = float(observation["source_timestamp"])
    same_timestamp = any(abs(float(row["source_timestamp"]) - timestamp) < 1e-9 for row in history)
    if same_timestamp:
        return history, False, "duplicate_source_timestamp"
    if history and timestamp < float(history[-1]["source_timestamp"]):
        return history, False, "out_of_order_source_timestamp"
    updated = [*history, observation]
    return updated[-limit:], True, None


def _prior_values(
    history: list[dict[str, Any]],
    source_timestamp: float,
) -> list[float]:
    return [
        float(row["paired_atm_iv"])
        for row in history
        if float(row["source_timestamp"]) < source_timestamp
    ]


def _public_instrument_rows(payload: Any) -> list[dict[str, Any]]:
    root = _mapping(payload)
    result = root.get("result", root)
    if not isinstance(result, list):
        return []
    return [dict(item) for item in result if isinstance(item, Mapping)]


def _active_option_row(row: Mapping[str, Any], now: float) -> bool:
    if str(row.get("instrument_type", "")).lower() != "option":
        return False
    if str(row.get("base_currency", "")).upper() != "SOL":
        return False
    if row.get("is_active") is not True:
        return False
    details = _mapping(row.get("option_details"))
    expiry = _epoch(details.get("expiry"))
    activation = _epoch(row.get("scheduled_activation"))
    deactivation = _epoch(row.get("scheduled_deactivation"))
    if expiry is None or activation is None or deactivation is None:
        return False
    return activation <= now and deactivation > now and expiry > now


def _option_type(row: Mapping[str, Any]) -> str:
    value = str(_mapping(row.get("option_details")).get("option_type", "")).upper()
    if value in {"CALL", "C"}:
        return "C"
    if value in {"PUT", "P"}:
        return "P"
    return ""


def _select_expiry(rows: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    groups: dict[float, dict[str, dict[float, dict[str, Any]]]] = {}
    for row in rows:
        if not _active_option_row(row, now):
            continue
        details = _mapping(row.get("option_details"))
        expiry = _epoch(details.get("expiry"))
        strike = _number(details.get("strike"))
        kind = _option_type(row)
        if expiry is None or strike is None or kind not in {"C", "P"}:
            continue
        dte = (expiry - now) / 86400.0
        if not 2.0 <= dte <= 14.0:
            continue
        by_kind = groups.setdefault(expiry, {"C": {}, "P": {}})
        by_kind[kind].setdefault(strike, row)

    candidates: list[dict[str, Any]] = []
    for expiry, by_kind in groups.items():
        common_strikes = sorted(set(by_kind["C"]).intersection(by_kind["P"]))
        if not common_strikes:
            continue
        candidates.append(
            {
                "expiry_timestamp": expiry,
                "expiry": _iso(expiry),
                "dte": (expiry - now) / 86400.0,
                "call_strikes": sorted(by_kind["C"]),
                "put_strikes": sorted(by_kind["P"]),
                "common_strikes": common_strikes,
                "by_kind": by_kind,
            }
        )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (abs(float(item["dte"]) - TARGET_DTE), item["expiry_timestamp"]),
    )


def _select_atm_pair(expiry: Mapping[str, Any], spot: float) -> dict[str, Any] | None:
    if spot <= 0:
        return None
    common = list(expiry.get("common_strikes", ()))
    if not common:
        return None
    strike = min(common, key=lambda value: (abs(float(value) - spot), float(value)))
    distance = abs(float(strike) - spot) / spot
    if distance > ATM_MAX_DISTANCE:
        return {
            "ready": False,
            "blocker": "ATM_STRIKE_DISTANCE_GT_5_PERCENT",
            "strike": float(strike),
            "distance_pct": distance * 100.0,
        }
    by_kind = _mapping(expiry.get("by_kind"))
    call = _mapping(_mapping(by_kind.get("C")).get(strike))
    put = _mapping(_mapping(by_kind.get("P")).get(strike))
    if not call or not put:
        return None
    return {
        "ready": True,
        "strike": float(strike),
        "distance_pct": distance * 100.0,
        "call": dict(call),
        "put": dict(put),
    }


def _iv_from_ticker(
    ticker: Mapping[str, Any],
    receipt_timestamp: float,
    instrument_name: str,
) -> dict[str, Any]:
    source_timestamp = _epoch(ticker.get("timestamp"))
    age_seconds = None if source_timestamp is None else receipt_timestamp - source_timestamp
    blockers: list[str] = []
    if source_timestamp is None:
        blockers.append("OPTION_SOURCE_TIMESTAMP_MISSING")
    else:
        if source_timestamp > receipt_timestamp + FUTURE_SOURCE_TOLERANCE_SECONDS:
            blockers.append("OPTION_SOURCE_TIMESTAMP_IN_FUTURE")
        if age_seconds is not None and age_seconds > OPTION_MAX_AGE_SECONDS:
            blockers.append("OPTION_TICKER_STALE")
        if age_seconds is not None and age_seconds < -FUTURE_SOURCE_TOLERANCE_SECONDS:
            blockers.append("OPTION_SOURCE_TIMESTAMP_IN_FUTURE")
    mark_paths = (
        "mark_iv",
        "i",
        "iv",
        "option_pricing.mark_iv",
        "option_pricing.i",
        "option_pricing.iv",
    )
    bid_paths = ("bid_iv", "option_pricing.bid_iv")
    ask_paths = ("ask_iv", "option_pricing.ask_iv")

    for field in mark_paths:
        value = _valid_iv(_path(ticker, field))
        if value is not None:
            return {
                "ready": not blockers,
                "iv": value,
                "source": "MARK_IV",
                "source_fields": [field],
                "source_timestamp": source_timestamp,
                "receipt_timestamp": receipt_timestamp,
                "age_seconds": age_seconds,
                "instrument_name": instrument_name,
                "blockers": blockers,
            }

    bid_values = [(field, _valid_iv(_path(ticker, field))) for field in bid_paths]
    ask_values = [(field, _valid_iv(_path(ticker, field))) for field in ask_paths]
    bid = next(((field, value) for field, value in bid_values if value is not None), None)
    ask = next(((field, value) for field, value in ask_values if value is not None), None)
    if bid is not None and ask is not None:
        return {
            "ready": not blockers,
            "iv": (bid[1] + ask[1]) / 2.0,
            "source": "BID_ASK_MIDPOINT",
            "source_fields": [bid[0], ask[0]],
            "source_timestamp": source_timestamp,
            "receipt_timestamp": receipt_timestamp,
            "age_seconds": age_seconds,
            "instrument_name": instrument_name,
            "blockers": blockers,
        }
    if bid is not None:
        return {
            "ready": not blockers,
            "iv": bid[1],
            "source": "BID_ONLY",
            "source_fields": [bid[0]],
            "source_timestamp": source_timestamp,
            "receipt_timestamp": receipt_timestamp,
            "age_seconds": age_seconds,
            "instrument_name": instrument_name,
            "blockers": blockers,
        }
    if ask is not None:
        return {
            "ready": not blockers,
            "iv": ask[1],
            "source": "ASK_ONLY",
            "source_fields": [ask[0]],
            "source_timestamp": source_timestamp,
            "receipt_timestamp": receipt_timestamp,
            "age_seconds": age_seconds,
            "instrument_name": instrument_name,
            "blockers": blockers,
        }

    blockers.append("OPTION_IV_INVALID_OR_MISSING")
    return {
        "ready": False,
        "iv": None,
        "source": "NONE",
        "source_fields": [],
        "source_timestamp": source_timestamp,
        "receipt_timestamp": receipt_timestamp,
        "age_seconds": age_seconds,
        "instrument_name": instrument_name,
        "blockers": _dedupe(blockers),
    }


async def _public_get(
    http: httpx.AsyncClient,
    path: str,
    params: Mapping[str, Any],
) -> tuple[Any, float, str | None]:
    receipt_timestamp = time.time()
    try:
        response = await http.get(
            f"{PUBLIC_API_BASE}/{path}",
            params=dict(params),
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
    except httpx.HTTPError as exc:
        return None, receipt_timestamp, f"PUBLIC_API_ERROR:{type(exc).__name__}"
    try:
        payload = response.json()
    except ValueError:
        return None, receipt_timestamp, f"PUBLIC_API_INVALID_JSON:{response.status_code}"
    if response.status_code >= 400:
        return None, receipt_timestamp, f"PUBLIC_API_HTTP_{response.status_code}"
    if isinstance(payload, Mapping) and payload.get("error"):
        error = _mapping(payload["error"])
        return None, receipt_timestamp, f"PUBLIC_API_ERROR:{error.get('message', 'error')}"
    if isinstance(payload, Mapping):
        return payload.get("result", payload), receipt_timestamp, None
    return payload, receipt_timestamp, None


async def _fetch_metadata(
    http: httpx.AsyncClient,
    cache: dict[str, Any],
    now: float,
    ttl: float,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    if cache.get("rows") and now < float(cache.get("expires_at", 0.0)):
        return list(cache["rows"]), True, None
    result, received_at, error = await _public_get(
        http,
        "public/get_instruments",
        {"currency": "SOL", "instrument_type": "option", "expired": "false"},
    )
    rows = _public_instrument_rows({"result": result})
    if error or not rows:
        return [], False, error or "PUBLIC_OPTIONS_METADATA_EMPTY"
    cache["rows"] = rows
    cache["expires_at"] = received_at + ttl
    return list(rows), False, None


async def _fetch_option_ticker(
    http: httpx.AsyncClient,
    instrument_name: str,
) -> dict[str, Any]:
    result, receipt_timestamp, error = await _public_get(
        http,
        "public/get_ticker",
        {"instrument_name": instrument_name},
    )
    if error:
        return {
            "ready": False,
            "instrument_name": instrument_name,
            "receipt_timestamp": receipt_timestamp,
            "blockers": [error],
        }
    ticker = _mapping(result)
    leg = _iv_from_ticker(ticker, receipt_timestamp, instrument_name)
    leg["ticker_timestamp"] = _epoch(ticker.get("timestamp"))
    return leg


async def _perpetual_snapshot(client: Any, config: Config) -> dict[str, Any]:
    if client is None:
        return {
            "ready": False,
            "connector": config.perpetual_connector,
            "trading_pair": config.perpetual_pair,
            "blockers": ["HUMMINGBOT_CLIENT_UNAVAILABLE"],
        }
    pairs = [config.perpetual_pair, *PERPETUAL_PAIR_CANDIDATES]
    seen: set[str] = set()
    last_error = "PERPETUAL_BBO_UNAVAILABLE"
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        receipt_timestamp = time.time()
        try:
            order_book = await client.market_data.get_order_book(
                config.perpetual_connector,
                pair,
                depth=1,
            )
            diagnostics = await client.market_data.get_order_book_diagnostics(
                config.perpetual_connector,
                pair,
            )
        except Exception as exc:
            last_error = f"PERPETUAL_BBO_ERROR:{type(exc).__name__}"
            continue
        bids = _mapping(order_book).get("bids", [])
        asks = _mapping(order_book).get("asks", [])
        bid = _price_level(bids[0]) if isinstance(bids, list) and bids else None
        ask = _price_level(asks[0]) if isinstance(asks, list) and asks else None
        source_timestamp = _epoch(_mapping(order_book).get("timestamp"))
        age_seconds = None if source_timestamp is None else receipt_timestamp - source_timestamp
        diag_root = _mapping(diagnostics)
        order_books = _mapping(diag_root.get("order_books"))
        marker_row = _mapping(order_books.get(pair))
        snapshot_uid = marker_row.get("snapshot_uid")
        last_update_id = marker_row.get(
            "last_update_id",
            marker_row.get("last_diff_uid"),
        )
        blockers: list[str] = []
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            blockers.append("PERPETUAL_BBO_INVALID")
        if source_timestamp is None:
            blockers.append("PERPETUAL_SOURCE_TIMESTAMP_MISSING")
        elif source_timestamp > receipt_timestamp + FUTURE_SOURCE_TOLERANCE_SECONDS:
            blockers.append("PERPETUAL_SOURCE_TIMESTAMP_IN_FUTURE")
        elif age_seconds is not None and age_seconds > PERPETUAL_MAX_AGE_SECONDS:
            blockers.append("PERPETUAL_BBO_STALE")
        if age_seconds is not None and age_seconds < -FUTURE_SOURCE_TOLERANCE_SECONDS:
            blockers.append("PERPETUAL_SOURCE_TIMESTAMP_IN_FUTURE")
        if snapshot_uid is None or last_update_id is None:
            blockers.append("PERPETUAL_BBO_MARKER_MISSING")
        mid = None if bid is None or ask is None else (bid + ask) / 2.0
        return {
            "ready": not blockers,
            "connector": config.perpetual_connector,
            "trading_pair": pair,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "source_timestamp": source_timestamp,
            "receipt_timestamp": receipt_timestamp,
            "age_seconds": age_seconds,
            "marker": {
                "snapshot_uid": snapshot_uid,
                "last_update_id": last_update_id,
            },
            "blockers": _dedupe(blockers),
        }
    return {
        "ready": False,
        "connector": config.perpetual_connector,
        "trading_pair": config.perpetual_pair,
        "blockers": [last_error],
    }


def _empty_signal() -> dict[str, Any]:
    return {
        "paired_atm_iv": None,
        "baseline_iv": None,
        "ratio": None,
        "state": "INITIALIZING",
        "mode": "NOT_READY",
        "history_count": 0,
        "prior_history_count": 0,
        "new_observation": False,
        "baseline_source": "prior_observations_only",
    }


def _base_snapshot(config: Config) -> dict[str, Any]:
    return {
        "routine": NAME,
        "display_name": DISPLAY_NAME,
        "as_of": time.time(),
        "sources": {
            "options": f"{PUBLIC_API_BASE}/public/get_instruments and public/get_ticker",
            "perpetual": "Hummingbot market_data.get_order_book + get_order_book_diagnostics",
            "options_credentials_used": False,
            "mainnet_public_only": True,
        },
        "perpetual": {
            "ready": False,
            "connector": config.perpetual_connector,
            "trading_pair": config.perpetual_pair,
            "blockers": [],
        },
        "options": {
            "metadata_cache_seconds": config.metadata_cache_seconds,
            "expiry_selection": None,
            "atm_pair": None,
            "call": None,
            "put": None,
        },
        "expiry": None,
        "dte": None,
        "strike": None,
        "contracts": {"call": None, "put": None},
        "signal": _empty_signal(),
        # Flat fields are the strategy-facing machine contract. Keep them
        # populated even when a tick is blocked before signal calculation.
        "signal_ready": False,
        "state": "INITIALIZING",
        "mode": "NOT_READY",
        "monitor_status": "RUNNING",
        "tick_count": 0,
        "grid_context": {
            "selected_mode": "NOT_READY",
            "selected": None,
            "presets": {**GRID_CONTEXTS},
            **GRID_COMMON,
        },
        "readiness": {
            "read_only": True,
            "perpetual_bbo": False,
            "perpetual_bbo_fresh": False,
            "perpetual_marker": False,
            "expiry_selection": False,
            "paired_atm": False,
            "call_iv": False,
            "put_iv": False,
            "history_minimum": False,
            "baseline": False,
            "state": False,
            "mode": False,
            "signal_ready": False,
        },
        "blockers": [],
        "history": {
            "minimum": MIN_HISTORY,
            "maximum": config.max_history,
            "count": 0,
            "new_observation": False,
            "dedupe_key": None,
            "tail": [],
        },
        "safety": {
            "orders_submitted": 0,
            "orders_cancelled": 0,
            "fills": 0,
            "position_changes": 0,
            "execution_enabled_change": False,
            "mainnet_armed_change": False,
            "read_only": True,
        },
    }


def _publish_machine_state(
    snapshot: dict[str, Any],
    *,
    monitor_status: str,
    tick_count: int,
) -> dict[str, Any]:
    """Expose a stable flat signal contract alongside the rich snapshot."""

    signal = _mapping(snapshot.get("signal"))
    readiness = _mapping(snapshot.get("readiness"))
    snapshot["signal_ready"] = bool(readiness.get("signal_ready"))
    snapshot["state"] = str(signal.get("state") or "INITIALIZING")
    snapshot["mode"] = str(signal.get("mode") or "NOT_READY")
    snapshot["monitor_status"] = monitor_status
    snapshot["tick_count"] = tick_count
    return snapshot


async def _collect_snapshot(
    client: Any,
    context: Any,
    config: Config,
    http: httpx.AsyncClient,
    metadata_cache: dict[str, Any],
    previous_state: str,
    strategy_adapter: Any | None,
) -> tuple[dict[str, Any], str]:
    snapshot = _base_snapshot(config)
    blockers: list[str] = []
    history, state_error = _load_history(context, config.max_history)
    if state_error:
        blockers.append(state_error)

    perp = await _perpetual_snapshot(client, config)
    snapshot["perpetual"] = perp
    blockers.extend(perp.get("blockers", ()))
    if not perp.get("ready"):
        snapshot["readiness"]["perpetual_bbo"] = False
        snapshot["readiness"]["perpetual_bbo_fresh"] = False
        snapshot["readiness"]["perpetual_marker"] = False
        snapshot["history"]["count"] = len(history)
        snapshot["history"]["tail"] = history[-10:]
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state

    snapshot["readiness"]["perpetual_bbo"] = True
    snapshot["readiness"]["perpetual_bbo_fresh"] = True
    snapshot["readiness"]["perpetual_marker"] = True

    now = time.time()
    metadata, cache_hit, metadata_error = await _fetch_metadata(
        http, metadata_cache, now, config.metadata_cache_seconds
    )
    snapshot["options"]["metadata_cache_hit"] = cache_hit
    snapshot["options"]["metadata_count"] = len(metadata)
    if metadata_error:
        blockers.append(metadata_error)
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state

    expiry = _select_expiry(metadata, now)
    if expiry is None:
        blockers.append("NO_ACTIVE_PAIRED_SOL_OPTION_EXPIRY_2_TO_14_DTE")
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state
    snapshot["options"]["expiry_selection"] = {
        key: value for key, value in expiry.items() if key not in {"by_kind"}
    }
    snapshot["readiness"]["expiry_selection"] = True
    snapshot["expiry"] = expiry["expiry"]
    snapshot["dte"] = expiry["dte"]

    atm = _select_atm_pair(expiry, float(perp["mid"]))
    if not atm:
        blockers.append("NO_PAIRED_SAME_STRIKE_ATM_OPTION")
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state
    snapshot["options"]["atm_pair"] = {
        key: value for key, value in atm.items() if key not in {"call", "put"}
    }
    snapshot["strike"] = atm.get("strike")
    snapshot["readiness"]["paired_atm"] = bool(atm.get("ready"))
    if not atm.get("ready"):
        blockers.append(str(atm.get("blocker", "ATM_PAIR_NOT_READY")))
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state

    call_name = str(_mapping(atm["call"]).get("instrument_name", ""))
    put_name = str(_mapping(atm["put"]).get("instrument_name", ""))
    if not call_name or not put_name:
        blockers.append("OPTION_CONTRACT_NAME_MISSING")
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state

    call_leg, put_leg = await asyncio.gather(
        _fetch_option_ticker(http, call_name),
        _fetch_option_ticker(http, put_name),
    )
    snapshot["options"]["call"] = call_leg
    snapshot["options"]["put"] = put_leg
    snapshot["contracts"] = {"call": call_name, "put": put_name}
    call_ok = bool(call_leg.get("ready"))
    put_ok = bool(put_leg.get("ready"))
    snapshot["readiness"]["call_iv"] = call_ok
    snapshot["readiness"]["put_iv"] = put_ok
    blockers.extend(call_leg.get("blockers", ()))
    blockers.extend(put_leg.get("blockers", ()))
    call_timestamp = _epoch(call_leg.get("source_timestamp"))
    put_timestamp = _epoch(put_leg.get("source_timestamp"))
    if not call_ok or not put_ok or call_timestamp is None or put_timestamp is None:
        snapshot["history"]["count"] = len(history)
        snapshot["history"]["tail"] = history[-10:]
        snapshot["blockers"] = _dedupe(blockers)
        return snapshot, previous_state
    last_call = max(
        (_epoch(row.get("call_source_timestamp")) for row in history),
        default=None,
    )
    last_put = max(
        (_epoch(row.get("put_source_timestamp")) for row in history),
        default=None,
    )
    if last_call is not None and call_timestamp < last_call:
        blockers.append("CALL_SOURCE_OUT_OF_ORDER")
    if last_put is not None and put_timestamp < last_put:
        blockers.append("PUT_SOURCE_OUT_OF_ORDER")
    source_timestamp = max(call_timestamp, put_timestamp)
    paired_iv = (float(call_leg["iv"]) + float(put_leg["iv"])) / 2.0
    observation = {
        "source_timestamp": source_timestamp,
        "paired_atm_iv": paired_iv,
        "expiry": expiry["expiry"],
        "strike": atm["strike"],
        "call_contract": call_name,
        "put_contract": put_name,
        "call_source_timestamp": call_timestamp,
        "put_source_timestamp": put_timestamp,
    }
    if "CALL_SOURCE_OUT_OF_ORDER" not in blockers and "PUT_SOURCE_OUT_OF_ORDER" not in blockers:
        history, new_observation, history_status = _update_history(
            history, observation, config.max_history
        )
        if history_status:
            if history_status == "out_of_order_source_timestamp":
                blockers.append("PAIRED_IV_SOURCE_OUT_OF_ORDER")
            else:
                blockers.append("DUPLICATE_IV_SOURCE_TIMESTAMP")
        else:
            storage_error = _save_history(context, history, config.max_history)
            if storage_error:
                blockers.append(storage_error)
    else:
        new_observation, history_status = False, "leg_out_of_order"

    prior = _prior_values(history, source_timestamp)
    baseline = statistics.median(prior) if len(prior) >= MIN_HISTORY else None
    ratio = None if baseline in (None, 0.0) else paired_iv / float(baseline)
    history_ready = len(prior) >= MIN_HISTORY
    state, mode = _hysteresis_state(
        ratio,
        previous_state,
        history_ready,
        strategy_adapter,
    )
    selected_grid = dict(GRID_CONTEXTS[mode]) if mode in GRID_CONTEXTS else None
    selected_grid = None if selected_grid is None else {**selected_grid, **GRID_COMMON}
    signal = {
        "paired_atm_iv": paired_iv,
        "baseline_iv": baseline,
        "ratio": ratio,
        "state": state,
        "mode": mode,
        "history_count": len(history),
        "prior_history_count": len(prior),
        "new_observation": bool(new_observation),
        "history_status": history_status,
        "baseline_source": "prior_observations_only",
        "source_timestamp": source_timestamp,
        "source_timestamp_iso": _iso(source_timestamp),
    }
    if not history_ready:
        blockers.append(f"IV_HISTORY_WARMUP:{len(prior)}/{MIN_HISTORY}")
    if baseline is None:
        blockers.append("IV_BASELINE_NOT_READY")
    if state == "INITIALIZING":
        blockers.append("IV_STATE_INITIALIZING")
    if mode == "NOT_READY":
        blockers.append("GRID_MODE_NOT_READY")
    snapshot["signal"] = signal
    snapshot["grid_context"] = {
        "selected_mode": mode,
        "selected": selected_grid,
        "presets": {**GRID_CONTEXTS},
        **GRID_COMMON,
    }
    snapshot["readiness"]["history_minimum"] = history_ready
    snapshot["readiness"]["baseline"] = baseline is not None
    snapshot["readiness"]["state"] = state != "INITIALIZING"
    snapshot["readiness"]["mode"] = mode != "NOT_READY"
    snapshot["readiness"]["signal_ready"] = bool(
        snapshot["readiness"]["perpetual_bbo"]
        and snapshot["readiness"]["perpetual_bbo_fresh"]
        and snapshot["readiness"]["perpetual_marker"]
        and snapshot["readiness"]["expiry_selection"]
        and snapshot["readiness"]["paired_atm"]
        and snapshot["readiness"]["call_iv"]
        and snapshot["readiness"]["put_iv"]
        and snapshot["readiness"]["history_minimum"]
        and snapshot["readiness"]["baseline"]
        and snapshot["readiness"]["state"]
        and snapshot["readiness"]["mode"]
    )
    snapshot["history"] = {
        "minimum": MIN_HISTORY,
        "maximum": config.max_history,
        "count": len(history),
        "new_observation": bool(new_observation),
        "dedupe_key": source_timestamp,
        "tail": history[-10:],
    }
    snapshot["blockers"] = _dedupe(blockers)
    return snapshot, state


def _test_expiry_and_atm() -> None:
    now = 1_800_000_000.0
    rows: list[dict[str, Any]] = []
    for expiry, strikes in (
        (now + 3 * 86400, (100.0, 110.0)),
        (now + 11 * 86400, (100.0, 110.0)),
    ):
        for strike in strikes:
            for kind in ("C", "P"):
                rows.append(
                    {
                        "instrument_type": "option",
                        "base_currency": "SOL",
                        "is_active": True,
                        "scheduled_activation": now - 10,
                        "scheduled_deactivation": expiry + 10,
                        "option_details": {
                            "expiry": expiry,
                            "strike": str(strike),
                            "option_type": kind,
                        },
                        "instrument_name": f"SOL-{int(expiry)}-{int(strike)}-{kind}",
                    }
                )
    selected = _select_expiry(rows, now)
    assert selected is not None and selected["dte"] == 3.0
    pair = _select_atm_pair(selected, 105.0)
    assert pair is not None and pair["strike"] == 100.0
    rejected = _select_atm_pair(selected, 200.0)
    assert rejected is not None and not rejected["ready"]


def _test_iv_sources_and_freshness() -> None:
    now = 1_800_000_000.0
    mark = _iv_from_ticker(
        {"timestamp": now - 1, "option_pricing": {"iv": 0.8, "bid_iv": 0.7, "ask_iv": 0.9}},
        now,
        "C",
    )
    assert mark["source"] == "MARK_IV" and mark["iv"] == 0.8
    midpoint = _iv_from_ticker(
        {"timestamp": now - 1, "option_pricing": {"iv": 20, "bid_iv": 0.7, "ask_iv": 0.9}},
        now,
        "C",
    )
    assert midpoint["source"] == "BID_ASK_MIDPOINT" and midpoint["iv"] == 0.8
    bid_only = _iv_from_ticker(
        {"timestamp": now - 1, "option_pricing": {"bid_iv": 0.7, "ask_iv": 20}},
        now,
        "C",
    )
    assert bid_only["source"] == "BID_ONLY"
    stale = _iv_from_ticker(
        {"timestamp": now - OPTION_MAX_AGE_SECONDS - 1, "option_pricing": {"iv": 0.8}},
        now,
        "C",
    )
    assert "OPTION_TICKER_STALE" in stale["blockers"] and not stale["ready"]
    future = _iv_from_ticker(
        {"timestamp": now + FUTURE_SOURCE_TOLERANCE_SECONDS + 1, "option_pricing": {"iv": 0.8}},
        now,
        "C",
    )
    assert "OPTION_SOURCE_TIMESTAMP_IN_FUTURE" in future["blockers"] and not future["ready"]


def _test_history_baseline_and_hysteresis() -> None:
    history: list[dict[str, Any]] = []
    for index in range(5):
        history, new, error = _update_history(
            history,
            {"source_timestamp": float(index + 1), "paired_atm_iv": 1.0},
            MAX_HISTORY,
        )
        assert new and error is None
    duplicate, new, error = _update_history(
        history,
        {"source_timestamp": 5.0, "paired_atm_iv": 9.0},
        MAX_HISTORY,
    )
    assert duplicate == history and not new and error == "duplicate_source_timestamp"
    _, new, error = _update_history(
        history,
        {"source_timestamp": 0.5, "paired_atm_iv": 1.0},
        MAX_HISTORY,
    )
    assert not new and error == "out_of_order_source_timestamp"
    bounded: list[dict[str, Any]] = []
    for index in range(MAX_HISTORY + 1):
        bounded, new, error = _update_history(
            bounded,
            {"source_timestamp": float(index + 1), "paired_atm_iv": 1.0},
            MAX_HISTORY,
        )
        assert new and error is None
    assert len(bounded) == MAX_HISTORY
    prior = _prior_values(history, 6.0)
    assert len(prior) == 5 and statistics.median(prior) == 1.0
    assert _hysteresis_state(None, "NORMAL", False)[0] == "INITIALIZING"
    assert _hysteresis_state(0.85, "INITIALIZING", True) == ("LOW", "AGGRESSIVE")
    assert _hysteresis_state(0.92, "LOW", True) == ("LOW", "AGGRESSIVE")
    assert _hysteresis_state(0.97, "LOW", True) == ("NORMAL", "NORMAL")
    assert _hysteresis_state(1.12, "NORMAL", True) == ("HIGH", "DEFENSIVE")
    assert _hysteresis_state(1.27, "HIGH", True) == ("EXTREME", "DEFENSIVE")
    assert _hysteresis_state(1.21, "EXTREME", True) == ("EXTREME", "DEFENSIVE")
    assert _hysteresis_state(1.20, "EXTREME", True) == ("HIGH", "DEFENSIVE")


def _test_read_only() -> None:
    for field in ("execution_enabled", "allow_mainnet_trading", "mainnet_armed"):
        try:
            Config(**{field: True})
        except ValueError:
            continue
        raise AssertionError(f"{field} was not rejected")


def _run_self_tests() -> dict[str, Any]:
    _test_expiry_and_atm()
    _test_iv_sources_and_freshness()
    _test_history_baseline_and_hysteresis()
    _test_read_only()
    return {
        "status": "PASS",
        "focused": {
            "expiry_selection": "PASS",
            "paired_atm_and_distance": "PASS",
            "iv_source_precedence": "PASS",
            "staleness_and_future": "PASS",
            "duplicate_and_out_of_order_history": "PASS",
            "causal_baseline": "PASS",
            "state_hysteresis_and_mode": "PASS",
            "read_only": "PASS",
        },
    }


def _machine_row(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    signal = _mapping(snapshot.get("signal"))
    readiness = _mapping(snapshot.get("readiness"))
    options = _mapping(snapshot.get("options"))
    call = _mapping(options.get("call"))
    put = _mapping(options.get("put"))
    contracts = _mapping(snapshot.get("contracts"))
    return {
        "routine": snapshot.get("routine"),
        "display_name": snapshot.get("display_name"),
        "expiry": snapshot.get("expiry"),
        "dte": snapshot.get("dte"),
        "strike": snapshot.get("strike"),
        "call_contract": contracts.get("call"),
        "put_contract": contracts.get("put"),
        "call_iv": call.get("iv"),
        "put_iv": put.get("iv"),
        "call_source": call.get("source"),
        "call_source_fields": call.get("source_fields"),
        "call_source_timestamp": call.get("source_timestamp"),
        "call_age_seconds": call.get("age_seconds"),
        "put_source": put.get("source"),
        "put_source_fields": put.get("source_fields"),
        "put_source_timestamp": put.get("source_timestamp"),
        "put_age_seconds": put.get("age_seconds"),
        "state": snapshot.get("state") or signal.get("state"),
        "mode": snapshot.get("mode") or signal.get("mode"),
        "paired_atm_iv": signal.get("paired_atm_iv"),
        "baseline_iv": signal.get("baseline_iv"),
        "ratio": signal.get("ratio"),
        "history_count": signal.get("history_count"),
        "new_observation": signal.get("new_observation"),
        "signal_ready": bool(snapshot.get("signal_ready", readiness.get("signal_ready"))),
        "monitor_status": snapshot.get("monitor_status", "RUNNING"),
        "tick_count": snapshot.get("tick_count", 0),
        "blockers": snapshot.get("blockers", []),
        "read_only": True,
    }


def _routine_result(snapshot: Mapping[str, Any]) -> Any:
    from routines.base import RoutineResult

    row = _machine_row(snapshot)
    return RoutineResult(
        text=json.dumps(dict(snapshot), sort_keys=True, default=str),
        table_data=[row],
        table_columns=list(row),
    )


def _test_snapshot(config: Config, tests: dict[str, Any]) -> dict[str, Any]:
    snapshot = _base_snapshot(config)
    snapshot["test_results"] = tests
    # Self-tests prove the routine implementation, not a live market signal.
    # Never publish a synthetic READY state that a strategy could consume.
    snapshot["readiness"]["signal_ready"] = False
    snapshot["blockers"] = ["TEST_MODE_NOT_LIVE"]
    snapshot["safety"]["test_mode"] = True
    return snapshot


def _report(report: Any, snapshot: Mapping[str, Any], stopped: bool = False) -> None:
    report.clear()
    builder = report.builder
    builder.source("routine", NAME)
    builder.tags(["derive", "sol", "options", "iv", "monitor", "read-only"])
    builder.section("01 / READ-ONLY STATUS", "Public Derive options IV and perpetual BBO.")
    signal = _mapping(snapshot.get("signal"))
    perp = _mapping(snapshot.get("perpetual"))
    options = _mapping(snapshot.get("options"))
    readiness = _mapping(snapshot.get("readiness"))
    builder.kpi("Monitor status", _text(snapshot.get("monitor_status")))
    builder.kpi("Refresh count", _text(snapshot.get("tick_count")))
    builder.kpi("Signal ready", "YES" if bool(snapshot.get("signal_ready")) else "NO")
    builder.kpi("Signal state", _text(snapshot.get("state") or signal.get("state")))
    builder.kpi("Mode", _text(snapshot.get("mode") or signal.get("mode")))
    builder.kpi("Paired ATM IV", _text(signal.get("paired_atm_iv")))
    builder.kpi(
        "Baseline / ratio", f"{_text(signal.get('baseline_iv'))} / {_text(signal.get('ratio'))}"
    )
    builder.kpi(
        "History / new observation",
        f"{_text(signal.get('history_count'))} / {_text(signal.get('new_observation'))}",
    )
    builder.kpi(
        "SOL BBO bid / ask / mid",
        f"{_text(perp.get('bid'))} / {_text(perp.get('ask'))} / {_text(perp.get('mid'))}",
    )
    builder.kpi(
        "BBO age / marker", f"{_age(perp.get('age_seconds'))} / {_text(perp.get('marker'))}"
    )
    builder.section(
        "02 / SELECTED OPTION PAIR",
        "Exact expiry, DTE, strike, contracts, IV source fields, timestamps and ages.",
    )
    call = _mapping(options.get("call"))
    put = _mapping(options.get("put"))
    option_row = {
        "Expiry": _text(snapshot.get("expiry")),
        "DTE": _text(snapshot.get("dte")),
        "Strike": _text(snapshot.get("strike")),
        "CALL contract": _text(snapshot.get("contracts", {}).get("call")),
        "CALL IV": _text(call.get("iv")),
        "CALL source": _text(call.get("source")),
        "CALL source fields": _text(call.get("source_fields")),
        "CALL source timestamp": _text(call.get("source_timestamp")),
        "CALL receipt timestamp": _text(call.get("receipt_timestamp")),
        "CALL age": _age(call.get("age_seconds")),
        "PUT contract": _text(snapshot.get("contracts", {}).get("put")),
        "PUT IV": _text(put.get("iv")),
        "PUT source": _text(put.get("source")),
        "PUT source fields": _text(put.get("source_fields")),
        "PUT source timestamp": _text(put.get("source_timestamp")),
        "PUT receipt timestamp": _text(put.get("receipt_timestamp")),
        "PUT age": _age(put.get("age_seconds")),
    }
    builder.table([option_row], list(option_row))
    builder.section(
        "03 / CAUSAL SIGNAL AND READINESS", "The baseline excludes the current observation."
    )
    builder.markdown(
        "Signal: "
        f"ready={_text(snapshot.get('signal_ready'))}, "
        f"state={_text(snapshot.get('state') or signal.get('state'))}, "
        f"mode={_text(snapshot.get('mode') or signal.get('mode'))}, "
        f"paired_iv={_text(signal.get('paired_atm_iv'))}, "
        f"baseline={_text(signal.get('baseline_iv'))}, ratio={_text(signal.get('ratio'))}, "
        f"history={_text(signal.get('history_count'))}, "
        f"new_observation={_text(signal.get('new_observation'))}."
    )
    readiness_row = {key: _text(value) for key, value in readiness.items()}
    builder.table([readiness_row], list(readiness_row))
    blockers = snapshot.get("blockers", [])
    builder.markdown(
        "Explicit blockers:\n"
        + ("\n".join(f"- {item}" for item in blockers) if blockers else "- None")
    )
    builder.section("04 / IMPLIED GRID CONTEXT", "Informational only; no executor is deployed.")
    grid = _mapping(snapshot.get("grid_context"))
    grid_rows = []
    for mode in ("AGGRESSIVE", "NORMAL", "DEFENSIVE"):
        row = dict(_mapping(grid.get("presets", {}).get(mode)))
        row.update({"mode": mode, "nominal_gap": "NOMINAL"})
        grid_rows.append(row)
    builder.table(
        grid_rows,
        [
            "mode",
            "half_width_pct",
            "full_width_pct",
            "levels",
            "nominal_gap_pct",
            "nominal_gap",
            "order_quote_usd",
        ],
    )
    builder.markdown(
        f"Selected mode: {_text(grid.get('selected_mode'))}. "
        f"TP={_pct(grid.get('take_profit_pct'))}; "
        f"stop_loss={_text(grid.get('stop_loss'))}; time_limit={_text(grid.get('time_limit'))}; "
        f"executor={_text(grid.get('executor'))}; "
        f"entry={_text(grid.get('entry_order_type'))}; "
        f"TP order={_text(grid.get('take_profit_order_type'))}."
    )
    builder.section(
        "05 / SAFETY",
        "The monitor never submits or cancels orders and never changes account state.",
    )
    builder.markdown(
        "- Public mainnet options API only; no options credentials used.\n"
        "- Perpetual reference is a fresh derive_perpetual BBO midpoint with tracker marker.\n"
        "- No trading mutation tool is called.\n"
        f"- stopped={stopped}; execution_enabled_change=False; mainnet_armed_change=False."
    )
    builder.manual_order()


async def run(config: Config, context: Any) -> dict[str, Any]:
    """Run one read-only tick or keep a LiveReport refreshed until stopped."""

    from config_manager import get_client

    from condor.reports import LiveReport

    report = LiveReport(
        DISPLAY_NAME,
        source_name=NAME,
        tags=["derive", "sol", "options", "iv", "monitor", "read-only"],
        auto_refresh_seconds=config.report_auto_refresh_seconds,
    )
    tick_count = 0
    snapshot = _publish_machine_state(
        _base_snapshot(config), monitor_status="RUNNING", tick_count=tick_count
    )
    if config.test_mode:
        tests = _run_self_tests()
        snapshot = _test_snapshot(config, tests)
        snapshot = _publish_machine_state(snapshot, monitor_status="TEST", tick_count=1)
        _report(report, snapshot)
        await report.update()
        report.builder.auto_refresh(None)
        await report.update()
        snapshot["report_id"] = report.report_id
        _persist_current_state(
            context,
            snapshot,
            monitor_status="TEST",
            tick_count=1,
            report_id=report.report_id,
        )
        return _routine_result(snapshot)

    metadata_cache: dict[str, Any] = {}
    strategy_adapter = _load_strategy_adapter()
    chat_id = getattr(context, "_chat_id", None)
    previous_state = _load_persisted_state(context)

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            while True:
                tick_count += 1
                try:
                    client = await get_client(chat_id, context=context)
                    snapshot, previous_state = await _collect_snapshot(
                        client,
                        context,
                        config,
                        http,
                        metadata_cache,
                        previous_state,
                        strategy_adapter,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    snapshot = _base_snapshot(config)
                    snapshot["blockers"] = [f"TICK_ERROR:{type(exc).__name__}:{exc}"]
                    snapshot["readiness"]["signal_ready"] = False

                snapshot = _publish_machine_state(
                    snapshot, monitor_status="RUNNING", tick_count=tick_count
                )
                state_error = _persist_current_state(
                    context,
                    snapshot,
                    monitor_status="RUNNING",
                    tick_count=tick_count,
                )
                if state_error:
                    snapshot["blockers"] = _dedupe(
                        [*snapshot.get("blockers", []), state_error]
                    )
                    snapshot["readiness"]["signal_ready"] = False
                    snapshot["signal_ready"] = False

                _report(report, snapshot)
                await report.update()
                snapshot["report_id"] = report.report_id
                _persist_current_state(
                    context,
                    snapshot,
                    monitor_status="RUNNING",
                    tick_count=tick_count,
                    report_id=report.report_id,
                )
                if config.run_once:
                    snapshot = _publish_machine_state(
                        snapshot, monitor_status="STOPPED", tick_count=tick_count
                    )
                    _report(report, snapshot, stopped=True)
                    report.builder.auto_refresh(None)
                    await report.update()
                    snapshot["report_id"] = report.report_id
                    _persist_current_state(
                        context,
                        snapshot,
                        monitor_status="STOPPED",
                        tick_count=tick_count,
                        report_id=report.report_id,
                    )
                    return _routine_result(snapshot)
                await asyncio.sleep(config.refresh_seconds)
    except asyncio.CancelledError:
        stopped_snapshot = snapshot if "snapshot" in locals() else _base_snapshot(config)
        stopped_snapshot = _publish_machine_state(
            stopped_snapshot, monitor_status="STOPPED", tick_count=tick_count
        )
        _persist_current_state(
            context,
            stopped_snapshot,
            monitor_status="STOPPED",
            tick_count=tick_count,
            report_id=report.report_id,
        )
        if report.report_id is not None:
            _report(report, stopped_snapshot, stopped=True)
            report.builder.auto_refresh(None)
            await report.update()
            stopped_snapshot["report_id"] = report.report_id
            _persist_current_state(
                context,
                stopped_snapshot,
                monitor_status="STOPPED",
                tick_count=tick_count,
                report_id=report.report_id,
            )
        return {
            "routine": NAME,
            "status": "STOPPED",
            "read_only": True,
            "report_id": report.report_id,
            "signal_ready": bool(stopped_snapshot.get("signal_ready")),
            "state": stopped_snapshot.get("state"),
            "mode": stopped_snapshot.get("mode"),
            "tick_count": tick_count,
            "blockers": stopped_snapshot.get("blockers", []),
            "safety": _base_snapshot(config)["safety"],
        }

