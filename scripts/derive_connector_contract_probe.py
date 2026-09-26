#!/usr/bin/env python3
"""Non-mutating probe for the installed Derive connector contract.

Run this with the exact Hummingbot Python used by a bot image. The request
builder is invoked with transport stubbed; it never signs, sends, cancels, or
changes an account. Instrument fee metadata is intentionally optional because
the static probe does not initialize a connector or make a public exchange
request. The running controller reports that metadata after initialization.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path


def _add_support_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    candidates = (
        repo_root / "src",
        repo_root / "controllers/market_making/derive_options_adaptive_grid_support",
        Path("/home/hummingbot/controllers/market_making/derive_options_adaptive_grid_support"),
    )
    for path in reversed(candidates):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derive-metadata-maker", type=Decimal, default=None)
    parser.add_argument("--derive-metadata-taker", type=Decimal, default=None)
    args = parser.parse_args()
    _add_support_path()

    from derive_options_adaptive_grid.connector_contract import (
        inspect_connector_contract,
        normalized_fee_observation,
    )

    snapshot = inspect_connector_contract()
    fee_module = importlib.import_module("hummingbot.core.utils.estimate_fee")
    schema = fee_module.TradeFeeSchemaLoader.configured_schema_for_exchange(
        "derive_perpetual"
    )
    maker_observation = normalized_fee_observation(schema.maker_percent_fee_decimal)
    taker_observation = normalized_fee_observation(schema.taker_percent_fee_decimal)
    normalized_maker = maker_observation["normalized_decimal_rate"]
    normalized_taker = taker_observation["normalized_decimal_rate"]
    fee_verified = None
    if args.derive_metadata_maker is not None and args.derive_metadata_taker is not None:
        fee_verified = bool(
            normalized_maker == args.derive_metadata_maker
            and normalized_taker == args.derive_metadata_taker
        )

    result = {
        "module_path": snapshot.module_path,
        "runtime_version": snapshot.runtime_version,
        "source_hash": snapshot.source_hash,
        "fee_source_hash": snapshot.fee_source_hash,
        "open_reduce_only": snapshot.open_reduce_only,
        "close_reduce_only": snapshot.close_contract_verified,
        "limit_tif": snapshot.limit_tif,
        "limit_maker_tif": snapshot.limit_maker_tif,
        "contract_verified": snapshot.contract_verified,
        "connector_contract": snapshot.as_dict(),
        "fee_model": {
            "raw_hummingbot_maker_fee": maker_observation["raw_value"],
            "raw_hummingbot_taker_fee": taker_observation["raw_value"],
            "raw_hummingbot_fee_unit": "decimal_fraction",
            "schema_maker_fee_unit": maker_observation["raw_unit"],
            "schema_taker_fee_unit": taker_observation["raw_unit"],
            "normalized_maker_fee_decimal": normalized_maker,
            "normalized_taker_fee_decimal": normalized_taker,
            "derive_metadata_maker_fee_decimal": args.derive_metadata_maker,
            "derive_metadata_taker_fee_decimal": args.derive_metadata_taker,
            "fee_verified": fee_verified,
            "note": (
                "metadata comparison is supplied only when captured by the initialized "
                "controller; this probe itself performs no exchange request"
            ),
        },
        "mutation_probe": (
            "transport stub only; no signing, network, order, cancel, "
            "or account mutation"
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if snapshot.contract_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
