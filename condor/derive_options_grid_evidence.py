# DERIVE_OPTIONS_ADAPTIVE_GRID_CONDOR_MANAGED
"""One-shot read-only audit snapshot for the SOL adaptive grid controller."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


def _load_diagnostic_snapshot():
    """Load the sibling helper under Condor's dynamic routine loader."""

    try:
        from .derive_options_grid_health import diagnostic_snapshot

        return diagnostic_snapshot
    except ImportError:
        module_name = "_derive_options_grid_health_shared"
        module = sys.modules.get(module_name)
        if module is None:
            path = Path(__file__).with_name("derive_options_grid_health.py")
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"unable to load {path}") from None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        return module.diagnostic_snapshot


diagnostic_snapshot = _load_diagnostic_snapshot()

CATEGORY = "Monitoring"
CONTINUOUS = False


class Config(BaseModel):
    bot_name: str = Field(default="derive-options-adaptive-grid-sol-three-mode-shadow")
    execution_enabled: bool = False

    @model_validator(mode="after")
    def read_only(self):
        if self.execution_enabled:
            raise ValueError("derive_options_grid_evidence is read-only")
        return self


async def run(config: Config, context: Any) -> dict[str, Any]:
    """Fetch one bot-status payload and preserve evidence classification."""

    if config.execution_enabled:
        raise RuntimeError("derive_options_grid_evidence refused execution_enabled=true")
    from config_manager import get_client

    chat_id = getattr(context, "_chat_id", None)
    client = await get_client(chat_id, context=context)
    payload: Any = {"status": "STOPPED", "error_logs": []}
    if client is not None:
        try:
            payload = await client.bot_orchestration.get_bot_status(config.bot_name)
        except Exception as exc:
            payload = {"status": "ERROR", "error_logs": [{"msg": f"{type(exc).__name__}: {exc}"}]}
    snapshot = diagnostic_snapshot(payload)
    reconciliation = snapshot.get("reconciliation", {})
    evidence_counts = snapshot.get("evidence", {}).get("counts", {})
    return {
        "bot_name": config.bot_name,
        "runtime": snapshot.get("runtime", {}),
        "execution_state": snapshot.get("execution_state", "UNKNOWN"),
        "execution": snapshot.get("execution", {}),
        "bbo_pricing": snapshot.get("pricing", {}),
        "iv_regime": {
            "options": snapshot.get("options", {}),
            "iv_state": snapshot.get("iv_state", {}),
            "market_state": snapshot.get("market_state", "UNKNOWN"),
            "intended_grid_mode": snapshot.get("intended_grid_mode", "UNKNOWN"),
        },
        "grid_plan": snapshot.get("grid", {}),
        "capital": snapshot.get("capital", {}),
        "entry_safety": snapshot.get("entry_safety", {}),
        "exit_safety": snapshot.get("exit_safety", {}),
        "connector_proof": snapshot.get("connector_proof", {}),
        "lifecycle_evidence": snapshot.get("lifecycle_evidence", {}),
        "fee_economics": snapshot.get("fee_economics", {}),
        "account_cleanliness": snapshot.get("account_cleanliness", {}),
        "risk_gates": snapshot.get("risk_gates", {}),
        "inventory": snapshot.get("inventory", {}),
        "executors": snapshot.get("executors", []),
        "evidence": snapshot.get("evidence", {}),
        "pnl": snapshot.get("pnl", {}),
        "calibration_capture": {
            "ready": snapshot.get("calibration_observation_ready", False),
            "observation": snapshot.get("calibration_observation"),
            "errors": snapshot.get("calibration_observation_errors", []),
        },
        "intended_grid_available": snapshot.get("intended_grid_available", False),
        "intended_grid_mode": snapshot.get("intended_grid_mode", "UNKNOWN"),
        "can_create_executor_now": snapshot.get("can_create_executor_now", False),
        "execution_blockers": snapshot.get("execution_blockers", []),
        "mode_reasons": snapshot.get("mode_reasons", []),
        "risk_blockers": snapshot.get("risk_blockers", []),
        "operator_blockers": snapshot.get("operator_blockers", []),
        "live_readiness": snapshot.get("live_readiness", {}),
        "reconciliation": reconciliation,
        "evidence_counts": evidence_counts,
        "snapshot": snapshot,
        "evidence_policy": {
            "real_fill_kind": "REAL_EXECUTOR_FILL",
            "real_fill_source": "native Hummingbot executor/account diagnostics only",
            "shadow_and_proxy_are_not_fills": True,
            "source": "Hummingbot bot_orchestration.get_bot_status",
        },
    }


__all__ = ["CATEGORY", "CONTINUOUS", "Config", "run"]
