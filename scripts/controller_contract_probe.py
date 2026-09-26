#!/usr/bin/env python3
"""Non-mutating Hummingbot loader/config contract probe."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_controller(path: Path):
    spec = importlib.util.spec_from_file_location("derive_options_adaptive_grid_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load controller from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", type=Path, default=None)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    controller = args.controller or (
        repo_root / "controllers/market_making/derive_options_adaptive_grid.py"
    )
    sys.path.insert(0, str(repo_root / "src"))
    module = load_controller(controller)
    config = module.DeriveOptionsAdaptiveGridConfig()
    invalid_checks: dict[str, str] = {}
    for name, override in {
        "wrong_pair": {"trading_pair": "BTC-USDC"},
        "wrong_connector": {"connector_name": "derive_perpetual_testnet"},
        "wrong_environment": {"environment": "testnet"},
        "wrong_position_mode": {"position_mode": "HEDGE"},
    }.items():
        try:
            module.DeriveOptionsAdaptiveGridConfig(**override)
        except Exception as exc:  # expected rejection
            invalid_checks[name] = type(exc).__name__
        else:
            invalid_checks[name] = "NOT_REJECTED"
    connector_proof_rejections: dict[str, str] = {}
    for name in (
        "connector_close_semantics_verified",
        "connector_post_only_semantics_verified",
        "connector_lifecycle_verified",
    ):
        try:
            module.DeriveOptionsAdaptiveGridConfig(**{name: True})
        except Exception as exc:  # config cannot assert runtime proof
            connector_proof_rejections[name] = type(exc).__name__
        else:
            connector_proof_rejections[name] = "NOT_REJECTED"
    result = {
        "controller": str(controller),
        "connector_contract": (
            module._cached_connector_contract().as_dict()
            if hasattr(module, "_cached_connector_contract")
            else {"contract_verified": False, "errors": ("not_exposed",)}
        ),
        "config": {
            "id": config.id,
            "connector_name": config.connector_name,
            "trading_pair": config.trading_pair,
            "exchange_instrument": config.exchange_instrument,
            "environment": config.environment,
            "position_mode": str(config.position_mode.value),
            "leverage": config.leverage,
            "mainnet_armed": config.mainnet_armed,
            "execution_enabled": config.execution_enabled,
            "manual_kill_switch": config.manual_kill_switch,
            "oneway_side": config.oneway_side.name,
            "capital_allocation_mode": config.capital_allocation_mode,
            "capital_reserve_pct": str(config.capital_reserve_pct),
            "capital_reserve_quote": str(config.capital_reserve_quote),
            "capital_utilization_pct": str(config.capital_utilization_pct),
            "capital_fee_buffer_pct": str(config.capital_fee_buffer_pct),
            "capital_fee_buffer_quote": str(config.capital_fee_buffer_quote),
            "hard_position_multiplier": str(config.hard_position_multiplier),
            "min_profit_buffer_bps": str(config.min_profit_buffer_bps),
            "static_quote_caps": {
                "total": str(config.total_amount_quote),
                "aggressive": str(config.aggressive_total_quote),
                "defensive": str(config.defensive_total_quote),
                "max_position": str(config.max_position_quote),
                "hard_position": str(config.hard_position_quote),
                "max_order_notional": str(config.max_order_notional_quote),
            },
            "connector_proof_configurable": False,
            "legacy_connector_proof_flags": {
                "close": config.connector_close_semantics_verified,
                "post_only": config.connector_post_only_semantics_verified,
                "lifecycle": config.connector_lifecycle_verified,
            },
            "time_limit_seconds": config.time_limit_seconds,
            "three_mode_invariant": "LOW->AGGRESSIVE, NORMAL->NORMAL, HIGH/EXTREME->DEFENSIVE",
        },
        "invalid_config_checks": invalid_checks,
        "connector_proof_true_rejections": connector_proof_rejections,
        "fee_model_contract": (
            "runtime fee-source mismatches must report UNKNOWN with no verified "
            "economic take-profit floor"
        ),
        "mutation_probe": (
            "not run; no market data provider, action queue, bot, or connector mutation was used"
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if all(
        value != "NOT_REJECTED"
        for value in (*invalid_checks.values(), *connector_proof_rejections.values())
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
