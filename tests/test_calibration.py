import pytest

from derive_options_adaptive_grid.models import MarketState
from derive_options_adaptive_grid.research.calibration import (
    CalibrationConfig,
    ThresholdPolicy,
    _holdout_evaluation,
    _monotonic_gate,
    _post_decision_touch_levels,
    _select_frozen_candidates,
    _selection_row_is_eligible,
    calibrate,
    classify_features,
    classify_ratio,
)
from derive_options_adaptive_grid.research.features import (
    HORIZON_SECONDS,
    CausalFeature,
    FeatureBuildResult,
    FeatureOutcomes,
    ForwardOutcome,
    chronological_split,
)
from derive_options_adaptive_grid.research.io import ObservationAudit
from derive_options_adaptive_grid.research.models import CalibrationObservation, PricePoint
from derive_options_adaptive_grid.research.reporting import write_report


def _row(index: int, iv: float = 0.5) -> CalibrationObservation:
    timestamp = float(index * 300)
    return CalibrationObservation.from_mapping(
        {
            "decision_timestamp": timestamp,
            "source_timestamp": timestamp - 2,
            "received_timestamp": timestamp - 1,
            "underlying": "SOL",
            "trading_pair": "SOL-USDC",
            "exchange_instrument": "SOL-PERP",
            "environment": "mainnet",
            "perp_mid": 100 + index * 0.1,
            "best_bid": 100 + index * 0.1 - 0.01,
            "best_ask": 100 + index * 0.1 + 0.01,
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
    )


def test_threshold_boundary_and_hysteresis_behavior():
    policy = ThresholdPolicy()
    assert classify_ratio(1.25, MarketState.NORMAL, policy) == MarketState.HIGH
    assert classify_ratio(1.60, MarketState.HIGH, policy) == MarketState.EXTREME
    assert classify_ratio(1.35, MarketState.EXTREME, policy) == MarketState.HIGH
    assert classify_ratio(1.12, MarketState.HIGH, policy) == MarketState.NORMAL


def test_hysteresis_is_classified_before_chronological_slicing():
    policy = ThresholdPolicy()
    items = tuple(
        FeatureOutcomes(
            CausalFeature(
                observation=_row(index + 1),
                prior_median_iv=1.0,
                iv_ratio=ratio,
            ),
            {},
        )
        for index, ratio in enumerate((1.60, 1.50, 1.10, 1.00))
    )
    classified = classify_features(items, policy)
    _, validation, _ = chronological_split(
        classified, development_fraction=0.25, validation_fraction=0.25
    )
    assert validation[0][1] == MarketState.EXTREME
    assert classify_features((validation[0][0],), policy)[0][1] == MarketState.HIGH


def test_touch_proxy_excludes_symmetric_center_level():
    assert _post_decision_touch_levels(100.0, 0.10, 5) == pytest.approx((90.0, 95.0, 105.0, 110.0))


def _state_outcome_row(
    *, normal_vol: float, high_vol: float, extreme_vol: float, horizon: str = "1h"
):
    row = {
        "normal_count": 6,
        "high_count": 6,
        "extreme_count": 6,
        f"normal_{horizon}_outcome_count": 6,
        f"high_{horizon}_outcome_count": 6,
        f"extreme_{horizon}_outcome_count": 6,
        f"normal_{horizon}_realized_volatility": normal_vol,
        f"high_{horizon}_realized_volatility": high_vol,
        f"extreme_{horizon}_realized_volatility": extreme_vol,
    }
    return row


def test_holdout_gate_reports_each_volatility_delta_and_rejects_non_monotonicity():
    passing = _state_outcome_row(normal_vol=0.10, high_vol=0.20, extreme_vol=0.20)
    passes, reasons, metrics = _monotonic_gate(passing, CalibrationConfig())
    assert passes
    assert reasons == ()
    assert metrics["high_minus_normal"] == pytest.approx(0.10)
    assert metrics["extreme_minus_high"] == pytest.approx(0.0)
    evaluation = _holdout_evaluation(
        {"candidate_id": "threshold-test"}, passing, CalibrationConfig()
    )
    assert evaluation["holdout_pass"]
    assert evaluation["holdout_metrics"] == metrics

    rejected = _state_outcome_row(normal_vol=0.10, high_vol=0.20, extreme_vol=0.15)
    evaluation = _holdout_evaluation(
        {"candidate_id": "threshold-test"}, rejected, CalibrationConfig()
    )
    assert not evaluation["holdout_pass"]
    assert "extreme_below_high" in evaluation["holdout_failure_reasons"]


def test_selection_eligibility_uses_the_horizon_selected_by_the_volatility_gate():
    row = _state_outcome_row(
        normal_vol=0.10,
        high_vol=0.20,
        extreme_vol=0.20,
        horizon="30m",
    )
    assert _selection_row_is_eligible(row, CalibrationConfig())


def _threshold_selection_row(policy: ThresholdPolicy, split: str) -> dict:
    return {
        "split": split,
        "policy_name": policy.name,
        "candidate_id": f"THRESHOLD:{policy.name}",
        "high_enter_ratio": policy.high_enter_ratio,
        "high_exit_ratio": policy.high_exit_ratio,
        "extreme_enter_ratio": policy.extreme_enter_ratio,
        "extreme_exit_ratio": policy.extreme_exit_ratio,
        **_state_outcome_row(normal_vol=0.10, high_vol=0.20, extreme_vol=0.20),
    }


def test_threshold_freezing_ignores_holdout_and_uses_irregular_grid_adjacency():
    base = ThresholdPolicy(
        high_enter_ratio=1.25,
        high_exit_ratio=1.10,
        extreme_enter_ratio=1.60,
        extreme_exit_ratio=1.35,
    )
    one_high_exit_step = ThresholdPolicy(
        high_enter_ratio=1.25,
        high_exit_ratio=1.12,
        extreme_enter_ratio=1.60,
        extreme_exit_ratio=1.35,
    )
    two_high_exit_steps = ThresholdPolicy(
        high_enter_ratio=1.25,
        high_exit_ratio=1.15,
        extreme_enter_ratio=1.60,
        extreme_exit_ratio=1.35,
    )
    config = CalibrationConfig(minimum_neighbor_count=1)
    dev_validation = [
        _threshold_selection_row(policy, split)
        for policy in (base, one_high_exit_step)
        for split in ("development", "validation")
    ]
    status, frozen, stability = _select_frozen_candidates(
        [*dev_validation, _threshold_selection_row(base, "holdout")], config
    )
    assert status == "FROZEN_FOR_HOLDOUT"
    assert frozen
    assert any(
        "high_exit_ratio" in dimensions
        for row in stability
        for dimensions in row["neighbor_dimensions_differed"].values()
    )

    one_extreme_exit_step = ThresholdPolicy(
        high_enter_ratio=1.25,
        high_exit_ratio=1.10,
        extreme_enter_ratio=1.60,
        extreme_exit_ratio=1.30,
    )
    _, _, exit_stability = _select_frozen_candidates(
        [
            _threshold_selection_row(policy, split)
            for policy in (base, one_extreme_exit_step)
            for split in ("development", "validation")
        ],
        config,
    )
    assert any(
        "extreme_exit_ratio" in dimensions
        for row in exit_stability
        for dimensions in row["neighbor_dimensions_differed"].values()
    )

    changed_holdout = _threshold_selection_row(base, "holdout")
    changed_holdout["high_1h_realized_volatility"] = 99.0
    changed_holdout["extreme_1h_realized_volatility"] = 0.01
    unchanged_status, unchanged_frozen, _ = _select_frozen_candidates(
        [*dev_validation, changed_holdout],
        config,
    )
    assert (status, frozen) == (unchanged_status, unchanged_frozen)

    non_adjacent_rows = [
        _threshold_selection_row(policy, split)
        for policy in (base, two_high_exit_steps)
        for split in ("development", "validation")
    ]
    status, frozen, _ = _select_frozen_candidates(non_adjacent_rows, config)
    assert status == "NO_ROBUST_CANDIDATE"
    assert frozen == ()


def _synthetic_pipeline_inputs(*, holdout_extreme_vol: float):
    observations = []
    features = []
    outcomes = []
    for index in range(90):
        observation = _row(index + 1)
        observations.append(observation)
        phase = index % 18
        iv_ratio = 1.0 if phase < 6 else 1.30 if phase < 12 else 1.80
        feature = CausalFeature(
            observation=observation,
            prior_median_iv=1.0,
            iv_ratio=iv_ratio,
        )
        features.append(feature)
        state_volatility = 0.10 if phase < 6 else 0.20
        if phase >= 12 and index >= 72:
            state_volatility = holdout_extreme_vol
        outcomes.append(
            FeatureOutcomes(
                feature=feature,
                outcomes={
                    horizon: ForwardOutcome(
                        horizon=horizon,
                        target_timestamp=observation.decision_timestamp + seconds,
                        signed_return=0.0,
                        absolute_return=0.01,
                        high_low_excursion=0.02,
                        realized_volatility=state_volatility,
                        max_favorable_excursion=0.01,
                        max_adverse_excursion=0.01,
                    )
                    for horizon, seconds in HORIZON_SECONDS.items()
                },
            )
        )
    audit = ObservationAudit(
        source_path=None,
        total_rows=len(observations),
        accepted_rows=len(observations),
        rejected_rows=0,
        incompatible_rows=0,
        rejection_reasons={},
        first_decision_timestamp=observations[0].decision_timestamp,
        last_decision_timestamp=observations[-1].decision_timestamp,
        evidence_counts={"SHADOW_PLAN": len(observations)},
    )
    feature_build = FeatureBuildResult(
        features=tuple(features),
        accepted_observations=tuple(observations),
        audit=audit,
    )
    prices = tuple(
        PricePoint(
            timestamp=observation.decision_timestamp,
            mid=observation.perp_mid,
        )
        for observation in observations
    )
    return feature_build, tuple(outcomes), prices


@pytest.mark.parametrize(
    ("holdout_extreme_vol", "expected_status"),
    ((0.15, "HOLDOUT_REJECTED"), (0.20, "CANDIDATE_FOR_SHADOW_VALIDATION")),
)
def test_full_pipeline_holdout_controls_final_status(
    monkeypatch, holdout_extreme_vol: float, expected_status: str
):
    feature_build, outcomes, prices = _synthetic_pipeline_inputs(
        holdout_extreme_vol=holdout_extreme_vol
    )
    monkeypatch.setattr(
        "derive_options_adaptive_grid.research.calibration.build_causal_features",
        lambda observations, **kwargs: feature_build,
    )
    monkeypatch.setattr(
        "derive_options_adaptive_grid.research.calibration.attach_forward_outcomes",
        lambda features, price_points: outcomes,
    )
    config = CalibrationConfig(
        minimum_feature_count=1,
        minimum_holdout_count=1,
        minimum_state_count=5,
        minimum_outcome_count=5,
        minimum_neighbor_count=1,
        max_frozen_candidates=1,
        normal_half_widths=(0.010,),
        defensive_half_widths=(0.025,),
        normal_levels=(3, 5),
        defensive_levels=(2, 3),
        defensive_quote_fractions=(0.30, 0.60),
    )
    report = calibrate(
        feature_build.accepted_observations,
        price_points=prices,
        config=config,
    )
    assert report.status == expected_status
    assert report.candidate_selection_status == expected_status
    assert report.recommendation == "KEEP_CURRENT_DEFAULTS"
    assert report.frozen_candidates
    assert report.holdout_results
    assert all(row["model"] == "current_defaults" for row in report.holdout_results[:1])


def test_insufficient_data_is_honest_and_preserves_defaults(tmp_path):
    report = calibrate([_row(index) for index in range(1, 11)])
    assert report.status == "INSUFFICIENT_DATA"
    assert report.recommendation == "KEEP_CURRENT_DEFAULTS"
    assert report.width_results
    assert any("causal features" in item for item in report.data_limitations)
    paths = write_report(report, tmp_path / "calibration")
    assert (tmp_path / "calibration" / "calibration_report.md").exists()
    assert paths


def test_fixture_without_robust_candidate_keeps_defaults_without_real_fills():
    rows = [_row(index, 0.5 if index % 7 else 0.8) for index in range(1, 46)]
    report = calibrate(
        rows,
        config=CalibrationConfig(minimum_feature_count=10, minimum_holdout_count=2),
    )
    assert report.status == "NO_ROBUST_CANDIDATE"
    assert report.candidate_selection_status == "NO_ROBUST_CANDIDATE"
    assert report.frozen_candidates == ()
    assert report.recommendation == "KEEP_CURRENT_DEFAULTS"
    assert report.feature_count > 10
    assert report.split_counts["holdout"] >= 2
    assert report.state_threshold_results
    assert all(row["candidate_id"] for row in report.state_threshold_results)
    assert report.width_results
    assert all(row["real_executor_fill_count"] == 0 for row in report.width_results)
    assert all("SIMULATED" in row["evidence_label"] for row in report.width_results)
    assert all("normal_5m_absolute_return" in row for row in report.state_threshold_results)
    assert report.grid_candidate_selection_status == "NOT_ECONOMICALLY_IDENTIFIABLE"
    assert report.frozen_grid_candidates == ()
    assert report.proxy_grid_candidates
    assert all(
        row["quote_fraction_identifiability"] == "NOT_ECONOMICALLY_IDENTIFIABLE"
        for row in report.width_results
    )
    assert all(
        row["candidate_family"] in {"GRID", "LEVEL_SIZE"}
        for row in (*report.width_results, *report.level_size_results)
    )
    level_3 = next(
        row
        for row in report.level_size_results
        if row["normal_levels"] == 3
        and row["defensive_levels"] == 3
        and row["defensive_quote_fraction"] == 0.60
        and row["split"] == "development"
    )
    level_5 = next(
        row
        for row in report.level_size_results
        if row["normal_levels"] == 5
        and row["defensive_levels"] == 3
        and row["defensive_quote_fraction"] == 0.60
        and row["split"] == "development"
    )
    assert level_3["eligible_level_count"] != level_5["eligible_level_count"]
