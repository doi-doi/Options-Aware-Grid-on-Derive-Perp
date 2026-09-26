import importlib.util
import sys
from pathlib import Path

import pytest

from condor.derive_options_grid_health import Config, diagnostic_snapshot, render_text


def _load_dynamic_routine(path: Path):
    spec = importlib.util.spec_from_file_location(f"agent_routine_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(**overrides):
    controller = {
        "asset": "SOL",
        "updated_at": 100.0,
        "perp_market_ready": True,
        "connector_ready": True,
        "options_data_available": True,
        "market_state": "NORMAL",
        "grid_mode": "NORMAL",
        "runtime": {"controller_status": "RUNNING"},
        "execution": {
            "mainnet_armed": False,
            "execution_enabled": False,
            "manual_kill_switch": False,
        },
        "perp": {
            "bid": "149.9",
            "ask": "150.1",
            "mid": "150",
            "spread_bps": "13.33333333333333333333333333",
            "age_seconds": 0.0,
            "pricing_ready": True,
            "bbo_marker": 100,
        },
        "options": {"atm_iv": 0.6, "baseline_iv": 0.5, "days_to_expiry": 7},
        "iv_state": {"current_iv": 0.6, "baseline_iv": 0.5, "iv_ratio": 1.2},
        "state_policy": {
            "high_enter_ratio": 1.25,
            "high_exit_ratio": 1.12,
            "extreme_enter_ratio": 1.60,
            "extreme_exit_ratio": 1.35,
            "invariant": "reviewed",
        },
        "mode": {"mode": "NORMAL"},
        "inventory": {"position_quote": "2", "available_collateral_quote": "100"},
        "grid": {
            "plan_version": "abcd",
            "oneway_side": "BUY",
            "valid": True,
            "effective_levels": 5,
            "selected_leg": {
                "start_price": "148",
                "end_price": "152",
                "max_open_orders": 5,
                "min_order_amount_quote": "15.20",
                "total_amount_quote": "50",
            },
        },
        "risk_gates": {
            "ready": True,
            "planning_ready": True,
            "state_ready": True,
            "pricing_ready": True,
            "connector_ready": True,
            "reasons": [],
        },
        "intended_grid_available": True,
        "intended_grid_mode": "NORMAL",
        "can_create_executor_now": False,
        "reconciliation": {"kept": 1, "stops": 0, "creates": 0},
        "pnl": {"real_executor_fill_count": 0, "realized_pnl_quote": "0"},
        "evidence": {"counts": {"PUBLIC_MARKET_DATA": 1}},
    }
    controller.update(overrides)
    return {"status": "RUNNING", "data": {"controller": controller}}


def _shadow_observation():
    return {
        "decision_timestamp": 1002.0,
        "source_timestamp": 1000.0,
        "received_timestamp": 1001.0,
        "underlying": "SOL",
        "trading_pair": "SOL-USDC",
        "exchange_instrument": "SOL-PERP",
        "environment": "mainnet",
        "perp_mid": 100.5,
        "best_bid": 100.0,
        "best_ask": 101.0,
        "atm_call_iv": 0.5,
        "atm_put_iv": 0.6,
        "atm_iv": 0.55,
        "expiry_timestamp": 1002.0 + 7 * 86_400,
        "days_to_expiry": 7.0,
        "atm_strike": 100.0,
        "call_strike": 100.0,
        "put_strike": 100.0,
        "call_instrument": "SOL-C",
        "put_instrument": "SOL-P",
        "call_iv_source": "call_mark_iv",
        "put_iv_source": "put_mark_iv",
        "option_reference_price": 100.5,
        "source": "derive_options_adaptive_grid_controller",
        "evidence": "SHADOW_PLAN",
    }


def _shadow_payload(observation=None, **overrides):
    controller = {
        "asset": "SOL",
        "updated_at": 1002.0,
        "runtime": {"controller_status": "RUNNING"},
        "perp_market_ready": True,
        "connector_ready": True,
        "options_data_available": True,
        "market_state": "NORMAL",
        "grid_mode": "NORMAL",
        "iv_state": {"iv_ratio": 1.1},
        "perp": {
            "bid": "100.0",
            "ask": "101.0",
            "mid": "100.5",
            "pricing_ready": True,
        },
        "execution": {
            "mainnet_armed": False,
            "execution_enabled": False,
            "manual_kill_switch": False,
        },
        "calibration_observation_ready": True,
        "calibration_observation": observation or _shadow_observation(),
        "calibration_observation_errors": [],
    }
    controller.update(overrides)
    return {"status": "RUNNING", "data": {"controller": controller}}


def test_condor_keeps_perp_and_options_readiness_separate():
    snapshot = diagnostic_snapshot(
        _payload(options_data_available=False, options={"atm_iv": None}), now=100.0
    )
    assert snapshot["perp_ready"] is True
    assert snapshot["options_ready"] is False
    assert "OPTIONS_NOT_READY" in snapshot["alerts"]
    assert "PERP_NOT_READY" not in snapshot["alerts"]


def test_condor_renders_inventory_grid_gates_and_pnl():
    snapshot = diagnostic_snapshot(_payload(), now=100.0)
    rendered = render_text(snapshot)
    assert "PERP_READY: True" in rendered
    assert "OPTIONS_READY: True" in rendered
    assert "CONNECTOR_READY: True" in rendered
    assert "State / mode: NORMAL / NORMAL" in rendered
    assert snapshot["inventory"]["position_quote"] == "2"
    assert snapshot["grid"]["plan_version"] == "abcd"
    assert snapshot["grid"]["lower_bound"] == "148"
    assert snapshot["grid"]["upper_bound"] == "152"
    assert snapshot["grid"]["min_order_amount_quote"] == "15.20"
    assert snapshot["pnl"]["real_executor_fill_count"] == 0
    assert snapshot["execution_mode"] == "SHADOW_RUNNING"
    assert snapshot["connector_proof"]["status"] == "UNVERIFIED"
    assert "Connector contract: close_reduce_only=False" in rendered
    assert "Fee model: status=UNKNOWN" in rendered
    assert "economic TP floor=UNKNOWN" in rendered
    assert "2.02" not in rendered


def test_condor_preserves_raw_fee_observations_without_claiming_a_floor():
    snapshot = diagnostic_snapshot(
        _payload(
            connector_proof={
                "status": "UNVERIFIED",
                "close_reduce_only": False,
                "limit_maker_post_only": False,
                "lifecycle": "UNVERIFIED",
            },
            fee_economics={
                "ready": False,
                "fee_model_status": "UNKNOWN",
                "fee_source_mismatch": True,
                "schema_maker_fee_pct": "0.01",
                "schema_taker_fee_pct": "0.03",
                "instrument_metadata_maker_fee_pct": "0.0001",
                "instrument_metadata_taker_fee_pct": "0.0003",
                "minimum_economic_take_profit_pct": None,
            },
        ),
        now=100.0,
    )
    rendered = render_text(snapshot)
    assert "Hummingbot maker/taker=0.01 / 0.03" in rendered
    assert "Derive maker/taker=0.0001 / 0.0003" in rendered
    assert "economic TP floor=UNKNOWN" in rendered


def test_condor_distinguishes_cashout_from_fully_enabled_execution():
    snapshot = diagnostic_snapshot(
        _payload(
            execution={
                "mainnet_armed": True,
                "execution_enabled": False,
                "manual_kill_switch": True,
            }
        ),
        now=100.0,
    )
    assert snapshot["execution_mode"] == "CASHED_OUT"
    assert snapshot["execution"]["mainnet_armed"] is True
    assert snapshot["execution"]["execution_enabled"] is False
    assert snapshot["execution"]["manual_kill_switch"] is True


def test_condor_surfaces_connector_not_ready_separately():
    snapshot = diagnostic_snapshot(_payload(connector_ready=False), now=100.0)
    assert snapshot["connector_ready"] is False
    assert "CONNECTOR_NOT_READY" in snapshot["alerts"]
    assert snapshot["overall"] == "DEGRADED_DATA"


def test_condor_surfaces_the_reviewed_state_policy():
    snapshot = diagnostic_snapshot(_payload(), now=100.0)
    assert snapshot["state_policy"]["high_enter_ratio"] == 1.25
    assert snapshot["state_policy"]["extreme_exit_ratio"] == 1.35


def test_condor_config_rejects_execution_mode():
    with pytest.raises(ValueError, match="read-only"):
        Config(execution_enabled=True)


def test_condor_health_exposes_calibration_capture_state():
    observation = _shadow_observation()
    snapshot = diagnostic_snapshot(
        _payload(
            calibration_observation_ready=True,
            calibration_observation=observation,
            calibration_observation_errors=[],
        ),
        now=100.0,
    )
    assert snapshot["calibration_observation_ready"] is True
    assert snapshot["calibration_observation"]["source_timestamp"] == 1000.0
    assert snapshot["calibration_observation_errors"] == []
    assert "CALIBRATION_CAPTURE_READY: True" in render_text(snapshot)


def test_condor_separates_informational_mode_reasons_from_live_blockers():
    snapshot = diagnostic_snapshot(
        _payload(
            mode_reasons=("iv_ratio_normal",),
            risk_blockers=("connector_close_semantics_unverified",),
            operator_blockers=("mainnet_not_armed",),
            capital={
                "available_collateral_quote": "98.90",
                "reserve_quote": "20",
                "deployable_quote": "78.90",
                "target_strategy_quote": "39.45",
                "fee_buffer_quote": "5",
                "capital_ready": True,
            },
            entry_safety={"semantics_ready": False},
            exit_safety={"semantics_ready": False},
            fee_economics={"ready": True},
            account_cleanliness={
                "position_zero": True,
                "active_orders_count": 0,
                "managed_executors_count": 0,
                "unmanaged_executors_count": 0,
                "clean": True,
                "blockers": (),
            },
            live_readiness={"status": "BLOCKED", "candidate_ready": False},
        ),
        now=100.0,
    )
    assert snapshot["mode_reasons"] == ["iv_ratio_normal"]
    assert "iv_ratio_normal" not in snapshot["execution_blockers"]
    assert "connector_close_semantics_unverified" in snapshot["execution_blockers"]
    assert snapshot["capital"]["deployable_quote"] == "78.90"
    assert snapshot["account_cleanliness"]["clean"] is True
    assert "Capital available / target / deployable: 98.90 / 39.45 / 78.90" in render_text(
        snapshot
    )


def test_shadow_capture_writes_deduplicates_orders_and_restarts(tmp_path):
    module = _load_dynamic_routine(
        Path(__file__).parents[1] / "condor/derive_options_grid_shadow_capture.py"
    )
    output_path = tmp_path / "observations.jsonl"
    writer = module.ShadowObservationWriter(output_path)
    payload = _shadow_payload(
        connector_proof={
            "status": "UNVERIFIED",
            "close_reduce_only": False,
            "limit_maker_post_only": False,
            "lifecycle": "UNVERIFIED",
        },
        fee_economics={
            "ready": False,
            "fee_model_status": "UNKNOWN",
            "fee_source_mismatch": True,
        },
    )

    assert module.capture_status(payload, writer) == "written"
    assert module.capture_status(payload, writer) == "duplicate"
    out_of_order = _shadow_observation()
    out_of_order.update(source_timestamp=999.0, received_timestamp=999.5, decision_timestamp=999.75)
    assert (
        module.capture_status(
            _shadow_payload(
                out_of_order,
                connector_proof={
                    "status": "UNVERIFIED",
                    "close_reduce_only": False,
                    "limit_maker_post_only": False,
                    "lifecycle": "UNVERIFIED",
                },
                fee_economics={
                    "ready": False,
                    "fee_model_status": "UNKNOWN",
                    "fee_source_mismatch": True,
                },
            ),
            writer,
        )
        == "out_of_order"
    )
    assert writer.counters.records_written == 1
    assert writer.counters.duplicates_skipped == 1
    assert writer.counters.invalid_or_unready_skipped == 1
    assert writer.counters.last_fee_model_status == "UNKNOWN"
    assert writer.counters.last_fee_source_mismatch is True
    assert writer.counters.last_connector_proof_status == "UNVERIFIED"
    assert writer.counters.last_connector_close_reduce_only is False
    assert writer.counters.last_connector_post_only is False
    assert writer.counters.last_connector_lifecycle == "UNVERIFIED"

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "native_order_id" not in lines[0]
    assert '"connector_proof"' in lines[0]
    restarted = module.ShadowObservationWriter(output_path)
    assert module.capture_status(payload, restarted) == "duplicate"
    assert restarted.counters.records_written == 0


def test_shadow_capture_rejects_unsafe_gates_and_execution_config(tmp_path):
    module = _load_dynamic_routine(
        Path(__file__).parents[1] / "condor/derive_options_grid_shadow_capture.py"
    )
    with pytest.raises(ValueError, match="read-only"):
        module.Config(execution_enabled=True)
    writer = module.ShadowObservationWriter(tmp_path / "observations.jsonl")
    assert (
        module.capture_status(
            _shadow_payload(
                execution={
                    "mainnet_armed": True,
                    "execution_enabled": False,
                    "manual_kill_switch": True,
                }
            ),
            writer,
        )
        == "unsafe_gates"
    )
    assert writer.counters.records_written == 0
    assert writer.counters.unsafe_gate_skipped == 1


def test_shadow_capture_source_has_no_mutation_methods():
    source = (Path(__file__).parents[1] / "condor/derive_options_grid_shadow_capture.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    for forbidden in ("create_executor", "stop_executor", "cancel_order", "place_order"):
        assert forbidden not in lowered


def test_evidence_routine_loads_under_condors_dynamic_agent_loader():
    module = _load_dynamic_routine(
        Path(__file__).parents[1] / "condor/derive_options_grid_evidence.py"
    )
    assert module.Config().execution_enabled is False
    assert callable(module.diagnostic_snapshot)
