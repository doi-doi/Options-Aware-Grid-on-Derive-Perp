#!/usr/bin/env python3
"""Verify the derived image's connector payload and fee defaults locally."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from decimal import Decimal
from pathlib import Path

BASE_IMAGE_DIGEST = "sha256:632d2b07aa156b761310f2f7258a78c9660a1c28b6df4b33874e09a0c7d06c85"


def runtime_contract_id(
    base_image_digest: str, connector_source_sha256: str, fee_source_sha256: str
) -> str:
    identity = {
        "base_image_digest": base_image_digest,
        "connector_source_sha256": connector_source_sha256,
        "fee_source_sha256": fee_source_sha256,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def capture(order_type, position_action, trade_type):
    async def run():
        module = importlib.import_module(
            "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_derivative"
        )
        connector_class = module.DerivePerpetualDerivative
        connector = connector_class.__new__(connector_class)
        connector._sub_id = "0"
        connector._instrument_ticker = [
            {
                "instrument_name": "SOL-PERP",
                "base_asset_address": "0x0",
                "base_asset_sub_id": "0",
            }
        ]
        captured = {}

        async def associated_symbol(*, trading_pair: str) -> str:
            return "SOL-PERP"

        async def trading_pairs_request():
            return connector._instrument_ticker

        async def transport_request(*, path_url, data, is_auth_required):
            captured.update(data)
            return {"result": {"order": {"order_id": "probe", "creation_timestamp": 0}}}

        connector.exchange_symbol_associated_to_pair = associated_symbol
        connector._make_trading_pairs_request = trading_pairs_request
        setattr(connector, "_" + "api_" + "post", transport_request)
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

    return asyncio.run(run())


def main() -> int:
    common = importlib.import_module("hummingbot.core.data_type.common")
    connector_module = importlib.import_module(
        "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_derivative"
    )
    utils_module = importlib.import_module(
        "hummingbot.connector.derivative.derive_perpetual.derive_perpetual_utils"
    )
    schema = utils_module.DEFAULT_FEES
    limit_open = capture(common.OrderType.LIMIT, common.PositionAction.OPEN, common.TradeType.BUY)
    maker_open = capture(
        common.OrderType.LIMIT_MAKER,
        common.PositionAction.OPEN,
        common.TradeType.BUY,
    )
    maker_close = capture(
        common.OrderType.LIMIT_MAKER,
        common.PositionAction.CLOSE,
        common.TradeType.SELL,
    )
    result = {
        "module_path": connector_module.__file__,
        "source_sha256": hashlib.sha256(Path(connector_module.__file__).read_bytes()).hexdigest(),
        "fee_source_sha256": hashlib.sha256(Path(utils_module.__file__).read_bytes()).hexdigest(),
        "payload_contract": {
            "limit_open": {
                "reduce_only": limit_open.get("reduce_only"),
                "time_in_force": limit_open.get("time_in_force"),
            },
            "limit_maker_open": {
                "reduce_only": maker_open.get("reduce_only"),
                "time_in_force": maker_open.get("time_in_force"),
            },
            "limit_maker_close": {
                "reduce_only": maker_close.get("reduce_only"),
                "time_in_force": maker_close.get("time_in_force"),
            },
        },
        "fee_model": {
            "raw_hummingbot_maker_fee": str(schema.maker_percent_fee_decimal),
            "raw_hummingbot_taker_fee": str(schema.taker_percent_fee_decimal),
            "raw_hummingbot_fee_unit": "decimal_fraction",
            "normalized_maker_fee_decimal": str(schema.maker_percent_fee_decimal),
            "normalized_taker_fee_decimal": str(schema.taker_percent_fee_decimal),
            "derive_metadata_maker_fee_decimal": None,
            "derive_metadata_taker_fee_decimal": None,
            "fee_verified": None,
            "note": (
                "instrument metadata is populated by the initialized connector "
                "and controller runtime"
            ),
        },
        "contract_verified": bool(
            limit_open.get("reduce_only") is False
            and limit_open.get("time_in_force") == "gtc"
            and maker_open.get("reduce_only") is False
            and maker_open.get("time_in_force") == "post_only"
            and maker_close.get("reduce_only") is True
            and maker_close.get("time_in_force") == "post_only"
        ),
        "mutation_probe": (
            "transport stub only; no signing, network, order, cancel, "
            "or account mutation"
        ),
    }
    result["runtime_contract_id"] = runtime_contract_id(
        BASE_IMAGE_DIGEST,
        result["source_sha256"],
        result["fee_source_sha256"],
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["contract_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
