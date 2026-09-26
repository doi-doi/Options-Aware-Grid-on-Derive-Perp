import math
from dataclasses import replace

import pytest

from derive_options_adaptive_grid.research.features import (
    YEAR_SECONDS,
    _realized_volatility,
    attach_forward_outcomes,
    build_causal_features,
    chronological_split,
)
from derive_options_adaptive_grid.research.io import (
    append_shadow_observation,
    audit_observations,
)
from derive_options_adaptive_grid.research.models import CalibrationObservation, PricePoint
from derive_options_adaptive_grid.research.policies import (
    WidthPolicy,
    expected_move_pct,
    iv_scaled_half_width,
    research_half_width,
    static_half_width,
)


def _observation(timestamp: float, iv: float, *, mid: float = 100.0, **overrides):
    values = {
        "decision_timestamp": timestamp,
        "source_timestamp": timestamp - 2,
        "received_timestamp": timestamp - 1,
        "underlying": "SOL",
        "trading_pair": "SOL-USDC",
        "exchange_instrument": "SOL-PERP",
        "environment": "mainnet",
        "perp_mid": mid,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "atm_call_iv": iv,
        "atm_put_iv": iv,
        "atm_iv": iv,
        "expiry_timestamp": timestamp + 7 * 86_400,
        "days_to_expiry": 7,
        "atm_strike": 100,
        "call_strike": 100,
        "put_strike": 100,
        "call_instrument": "SOL-TEST-C",
        "put_instrument": "SOL-TEST-P",
        "iv_source": "mark_iv",
        "call_iv_source": "call_mark_iv",
        "put_iv_source": "put_mark_iv",
        "option_reference_price": 100,
        "source": "test",
        "evidence": "SHADOW_PLAN",
    }
    values.update(overrides)
    return CalibrationObservation.from_mapping(values)


def test_current_iv_is_excluded_from_causal_baseline():
    rows = [_observation(float(index), 0.5) for index in range(1, 6)]
    rows.append(_observation(6.0, 1.0))
    result = build_causal_features(rows, min_history=5)
    assert result.features[0].prior_median_iv == 0.5
    assert result.features[0].iv_ratio == 1.0
    assert result.features[1].prior_median_iv == 0.5
    assert result.features[1].iv_ratio == 2.0


def test_missing_iv_is_rejected_instead_of_forward_filled():
    rows = [_observation(float(index), 0.5) for index in range(1, 6)]
    rows.append(_observation(6.0, 0.9, atm_call_iv=None, atm_put_iv=None, atm_iv=None))
    accepted, audit = audit_observations(rows)
    assert len(accepted) == 5
    assert audit.rejected_rows == 1
    assert "same_strike_call_put_iv_required" in audit.rejection_reasons


def test_future_and_out_of_order_observations_are_rejected():
    first = _observation(10.0, 0.5)
    future = _observation(11.0, 0.5, source_timestamp=12.0, received_timestamp=12.5)
    duplicate = _observation(9.5, 0.5, source_timestamp=9.0, received_timestamp=9.5)
    accepted, audit = audit_observations([first, future, duplicate])
    assert accepted == [first]
    assert audit.rejected_rows == 2
    assert "source_after_decision" in audit.rejection_reasons
    assert "decision_timestamp_not_strictly_increasing" in audit.rejection_reasons


def test_stale_source_timestamp_is_rejected():
    row = _observation(100.0, 0.5, source_timestamp=1.0, received_timestamp=2.0)
    accepted, audit = audit_observations([row])
    assert accepted == []
    assert audit.rejection_reasons["source_timestamp_stale"] == 1


def test_missing_identity_is_incompatible_for_calibration():
    row = _observation(100.0, 0.5, underlying=None)
    accepted, audit = audit_observations([row])
    assert accepted == []
    assert audit.incompatible_rows == 1
    assert audit.rejection_reasons["INCOMPATIBLE_FOR_CALIBRATION"] == 1
    assert audit.rejection_reasons["underlying_missing"] == 1


def test_independent_iv_sources_and_atm_reference_distance_are_required():
    missing_put_source = _observation(100.0, 0.5, put_iv_source=None)
    accepted, audit = audit_observations([missing_put_source])
    assert accepted == []
    assert audit.rejection_reasons["put_iv_source_missing"] == 1

    distant_atm = _observation(101.0, 0.5, option_reference_price=120.0)
    accepted, audit = audit_observations([distant_atm])
    assert accepted == []
    assert audit.rejection_reasons["atm_distance_above_5pct"] == 1


def test_atm_reference_distance_boundary_is_inclusive_and_sources_round_trip():
    exactly_at_boundary = _observation(
        100.0,
        0.5,
        atm_strike=105.0,
        call_strike=105.0,
        put_strike=105.0,
        option_reference_price=100.0,
    )
    accepted, audit = audit_observations([exactly_at_boundary])
    assert accepted == [exactly_at_boundary]
    assert audit.rejected_rows == 0

    round_tripped = CalibrationObservation.from_mapping(exactly_at_boundary.to_dict())
    assert round_tripped.call_iv_source == "call_mark_iv"
    assert round_tripped.put_iv_source == "put_mark_iv"
    assert round_tripped.option_reference_price == pytest.approx(100.0)


def test_evidence_vocabulary_and_native_fill_identity_are_fail_closed():
    unknown = _observation(100.0, 0.5, evidence="MADE_UP_EVIDENCE")
    accepted, audit = audit_observations([unknown])
    assert accepted == []
    assert audit.rejection_reasons["evidence_unknown"] == 1

    real_fill_without_order_id = _observation(101.0, 0.5, evidence="REAL_EXECUTOR_FILL")
    accepted, audit = audit_observations([real_fill_without_order_id])
    assert accepted == []
    assert audit.rejection_reasons["native_order_id_required_for_real_fill"] == 1

    real_fill_with_order_id = _observation(
        102.0,
        0.5,
        evidence="REAL_EXECUTOR_FILL",
        native_order_id="native-order-1",
    )
    accepted, audit = audit_observations([real_fill_with_order_id])
    assert accepted == [real_fill_with_order_id]
    assert audit.evidence_counts == {"REAL_EXECUTOR_FILL": 1}


def test_shadow_append_validates_and_rejects_duplicate_or_out_of_order(tmp_path):
    path = tmp_path / "observations.jsonl"
    first = _observation(100.0, 0.5)
    append_shadow_observation(path, first)

    with pytest.raises(ValueError, match="invalid shadow observation"):
        append_shadow_observation(path, replace(first, call_strike=101.0))
    with pytest.raises(ValueError, match="duplicate or out of order"):
        append_shadow_observation(
            path,
            replace(
                first,
                decision_timestamp=99.0,
                source_timestamp=97.0,
                received_timestamp=98.0,
                expiry_timestamp=99.0 + 7 * 86_400,
            ),
        )

    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_realized_volatility_uses_all_log_returns_and_elapsed_time():
    prices = (100.0, 110.0, 99.0)
    elapsed_seconds = 600.0
    expected = math.sqrt(
        (math.log(prices[1] / prices[0]) ** 2 + math.log(prices[2] / prices[1]) ** 2)
        * YEAR_SECONDS
        / elapsed_seconds
    )
    assert _realized_volatility(prices, elapsed_seconds) == pytest.approx(expected)


def test_forward_outcomes_use_future_price_only():
    feature = build_causal_features(
        [_observation(float(index), 0.5, mid=100 + index) for index in range(1, 7)],
        min_history=2,
    ).features[0]
    outcomes = attach_forward_outcomes(
        [feature],
        [
            PricePoint(timestamp=feature.observation.decision_timestamp, mid=101),
            PricePoint(timestamp=feature.observation.decision_timestamp + 100, mid=150),
            PricePoint(timestamp=feature.observation.decision_timestamp + 300, mid=90),
        ],
        horizons={"5m": 300},
    )
    assert (
        outcomes[0].outcomes["5m"].target_timestamp == feature.observation.decision_timestamp + 300
    )
    assert outcomes[0].outcomes["5m"].signed_return < 0


def test_chronological_split_preserves_order():
    development, validation, holdout = chronological_split(tuple(range(10)))
    assert development == tuple(range(6))
    assert validation == (6, 7)
    assert holdout == (8, 9)


def test_width_policies_and_clamps():
    expected = expected_move_pct(0.60, 3600)
    assert expected > 0
    assert (
        iv_scaled_half_width(
            0.60,
            width_horizon_seconds=3600,
            width_sigma_multiplier=100,
            min_half_width_pct=0.005,
            max_half_width_pct=0.02,
        )
        == 0.02
    )
    assert (
        static_half_width("NORMAL", normal_half_width_pct=0.01, defensive_half_width_pct=0.025)
        == 0.01
    )
    width, expected_move = research_half_width(
        WidthPolicy.IV_SCALED,
        "HIGH",
        atm_iv=0.60,
        normal_half_width_pct=0.01,
        defensive_half_width_pct=0.025,
        width_horizon_seconds=3600,
        width_sigma_multiplier=1.5,
        min_half_width_pct=0.005,
        max_half_width_pct=0.05,
    )
    assert width > 0
    assert expected_move == expected
