"""Reproducible causal threshold, width, level, and size experiments."""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from statistics import mean, median
from typing import Any

from ..models import MarketState
from .features import (
    HORIZON_SECONDS,
    CausalFeature,
    FeatureOutcomes,
    attach_forward_outcomes,
    build_causal_features,
    chronological_split,
)
from .io import ObservationAudit
from .models import CalibrationObservation, PricePoint
from .policies import WidthPolicy, research_half_width


@dataclass(frozen=True, slots=True)
class ThresholdPolicy:
    aggressive_enter_ratio: float = 0.85
    aggressive_exit_ratio: float = 0.95
    high_enter_ratio: float = 1.25
    high_exit_ratio: float = 1.12
    extreme_enter_ratio: float = 1.60
    extreme_exit_ratio: float = 1.35

    def __post_init__(self) -> None:
        if not (
            0
            < self.aggressive_enter_ratio
            < self.aggressive_exit_ratio
            <= 1.0
            <= self.high_exit_ratio
            < self.high_enter_ratio
            <= self.extreme_exit_ratio
            < self.extreme_enter_ratio
        ):
            raise ValueError(
                "threshold policy must satisfy "
                "0 < aggressive_enter < aggressive_exit <= 1.0 <= high_exit "
                "< high_enter <= extreme_exit < extreme_enter"
            )

    @property
    def name(self) -> str:
        return (
            f"a{self.aggressive_enter_ratio:.2f}-h{self.high_enter_ratio:.2f}-"
            f"x{self.extreme_enter_ratio:.2f}-"
            f"he{self.high_exit_ratio:.2f}-xe{self.extreme_exit_ratio:.2f}"
        )


@dataclass(frozen=True, slots=True)
class ResearchCandidate:
    """One complete offline grid candidate; never a live configuration."""

    family: str
    threshold_policy: ThresholdPolicy
    width_policy: WidthPolicy
    normal_half_width_pct: float
    defensive_half_width_pct: float
    width_horizon_seconds: float
    width_sigma_multiplier: float
    min_half_width_pct: float
    max_half_width_pct: float
    normal_levels: int
    defensive_levels: int
    defensive_quote_fraction: float
    aggressive_half_width_pct: float = 0.0075
    aggressive_levels: int = 7

    @property
    def candidate_id(self) -> str:
        return (
            f"{self.family}:{self.threshold_policy.name}:"
            f"{self.width_policy.value}:"
            f"aw{self.aggressive_half_width_pct:.4f}:"
            f"nw{self.normal_half_width_pct:.4f}:dw{self.defensive_half_width_pct:.4f}:"
            f"al{self.aggressive_levels}:"
            f"nl{self.normal_levels}:dl{self.defensive_levels}:"
            f"q{self.defensive_quote_fraction:.2f}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_family": self.family,
            "width_policy": self.width_policy.value,
            "normal_half_width_pct": self.normal_half_width_pct,
            "defensive_half_width_pct": self.defensive_half_width_pct,
            "aggressive_half_width_pct": self.aggressive_half_width_pct,
            "width_horizon_seconds": self.width_horizon_seconds,
            "width_sigma_multiplier": self.width_sigma_multiplier,
            "min_half_width_pct": self.min_half_width_pct,
            "max_half_width_pct": self.max_half_width_pct,
            "normal_levels": self.normal_levels,
            "defensive_levels": self.defensive_levels,
            "aggressive_levels": self.aggressive_levels,
            "defensive_quote_fraction": self.defensive_quote_fraction,
            "high_enter_ratio": self.threshold_policy.high_enter_ratio,
            "high_exit_ratio": self.threshold_policy.high_exit_ratio,
            "extreme_enter_ratio": self.threshold_policy.extreme_enter_ratio,
            "extreme_exit_ratio": self.threshold_policy.extreme_exit_ratio,
        }


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    min_history: int = 5
    max_history: int = 120
    max_source_age_seconds: float | None = 15.0
    development_fraction: float = 0.60
    validation_fraction: float = 0.20
    minimum_feature_count: int = 30
    minimum_holdout_count: int = 5
    minimum_state_count: int = 5
    minimum_outcome_count: int = 5
    minimum_neighbor_count: int = 2
    minimum_proxy_sample_count: int = 5
    max_frozen_candidates: int = 3
    monotonic_tolerance: float = 0.0
    proxy_stability_tolerance: float = 0.25
    normal_half_widths: tuple[float, ...] = (0.005, 0.0075, 0.010, 0.0125, 0.015, 0.020)
    aggressive_half_widths: tuple[float, ...] = (0.005, 0.0075, 0.010)
    defensive_half_widths: tuple[float, ...] = (0.015, 0.020, 0.025, 0.030, 0.040, 0.050)
    aggressive_levels: tuple[int, ...] = (5, 7, 9)
    normal_levels: tuple[int, ...] = (3, 5, 7)
    defensive_levels: tuple[int, ...] = (2, 3, 5)
    defensive_quote_fractions: tuple[float, ...] = (0.30, 0.40, 0.50, 0.60, 0.75)
    normal_total_quote: float = 100.0
    width_horizon_seconds: float = 3600.0
    width_sigma_multiplier: float = 1.5
    min_half_width_pct: float = 0.005
    max_half_width_pct: float = 0.050
    current_thresholds: ThresholdPolicy = field(default_factory=ThresholdPolicy)


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    status: str
    recommendation: str
    audit: ObservationAudit
    feature_count: int
    split_counts: dict[str, int]
    data_limitations: tuple[str, ...]
    candidate_selection_status: str
    frozen_candidates: tuple[dict[str, Any], ...]
    neighbor_stability: tuple[dict[str, Any], ...]
    grid_candidate_selection_status: str
    frozen_grid_candidates: tuple[dict[str, Any], ...]
    proxy_grid_candidates: tuple[dict[str, Any], ...]
    grid_neighbor_stability: tuple[dict[str, Any], ...]
    current_default_metrics: dict[str, Any]
    regime_statistics: tuple[dict[str, Any], ...]
    state_threshold_results: tuple[dict[str, Any], ...]
    width_results: tuple[dict[str, Any], ...]
    level_size_results: tuple[dict[str, Any], ...]
    holdout_results: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return mean(values) if values else None


def classify_ratio(ratio: float, previous: MarketState, policy: ThresholdPolicy) -> MarketState:
    """Mirror the live hysteresis comparisons, including equality boundaries."""

    if previous == MarketState.EXTREME and ratio > policy.extreme_exit_ratio:
        return MarketState.EXTREME
    if ratio >= policy.extreme_enter_ratio:
        return MarketState.EXTREME
    if previous == MarketState.HIGH and ratio > policy.high_exit_ratio:
        return MarketState.HIGH
    if ratio >= policy.high_enter_ratio:
        return MarketState.HIGH
    if previous == MarketState.LOW and ratio < policy.aggressive_exit_ratio:
        return MarketState.LOW
    if ratio <= policy.aggressive_enter_ratio:
        return MarketState.LOW
    return MarketState.NORMAL


def classify_features(
    features: Sequence[FeatureOutcomes],
    policy: ThresholdPolicy,
    *,
    initial_state: MarketState = MarketState.NORMAL,
) -> tuple[tuple[FeatureOutcomes, MarketState], ...]:
    state = initial_state
    classified: list[tuple[FeatureOutcomes, MarketState]] = []
    for item in features:
        state = classify_ratio(item.feature.iv_ratio, state, policy)
        classified.append((item, state))
    return tuple(classified)


def _episode_statistics(
    classified: Sequence[tuple[FeatureOutcomes, MarketState]],
) -> dict[str, dict[str, float | int | None]]:
    episodes: dict[MarketState, list[float]] = {
        state: []
        for state in (MarketState.LOW, MarketState.NORMAL, MarketState.HIGH, MarketState.EXTREME)
    }
    if not classified:
        return {
            state.value.lower(): {
                "episode_count": 0,
                "total_duration_seconds": 0.0,
                "mean_episode_duration_seconds": None,
                "median_episode_duration_seconds": None,
            }
            for state in episodes
        }

    current_state = classified[0][1]
    current_duration = 0.0
    for index, (item, state) in enumerate(classified):
        if state != current_state:
            episodes[current_state].append(current_duration)
            current_state = state
            current_duration = 0.0
        if index + 1 < len(classified):
            next_time = classified[index + 1][0].feature.observation.decision_timestamp
            current_duration += max(0.0, next_time - item.feature.observation.decision_timestamp)
    episodes[current_state].append(current_duration)

    result: dict[str, dict[str, float | int | None]] = {}
    for state, durations in episodes.items():
        result[state.value.lower()] = {
            "episode_count": len(durations),
            "total_duration_seconds": sum(durations),
            "mean_episode_duration_seconds": mean(durations) if durations else None,
            "median_episode_duration_seconds": (median(durations) if durations else None),
        }
    return result


def _state_metrics(
    classified: Sequence[tuple[FeatureOutcomes, MarketState]],
    policy: ThresholdPolicy,
    *,
    split: str,
) -> dict[str, Any]:
    states = (MarketState.LOW, MarketState.NORMAL, MarketState.HIGH, MarketState.EXTREME)
    counts = {state.value: 0 for state in states}
    durations = {state.value: 0.0 for state in states}
    transitions = 0
    previous_state: MarketState | None = None
    for index, (item, state) in enumerate(classified):
        counts[state.value] += 1
        if previous_state is not None and previous_state != state:
            transitions += 1
        previous_state = state
        if index + 1 < len(classified):
            next_time = classified[index + 1][0].feature.observation.decision_timestamp
            duration = max(0.0, next_time - item.feature.observation.decision_timestamp)
            durations[state.value] += duration
    episodes = _episode_statistics(classified)
    total_duration = sum(durations.values())
    observation_count = len(classified)
    episode_means = [
        duration
        for state in episodes.values()
        if (duration := state["mean_episode_duration_seconds"]) is not None
    ]
    row: dict[str, Any] = {
        "split": split,
        "policy_name": policy.name,
        "aggressive_enter_ratio": policy.aggressive_enter_ratio,
        "aggressive_exit_ratio": policy.aggressive_exit_ratio,
        "high_enter_ratio": policy.high_enter_ratio,
        "high_exit_ratio": policy.high_exit_ratio,
        "extreme_enter_ratio": policy.extreme_enter_ratio,
        "extreme_exit_ratio": policy.extreme_exit_ratio,
        "observation_count": observation_count,
        "transition_count": transitions,
        "observed_duration_seconds": total_duration,
        "average_episode_duration_seconds": (mean(episode_means) if episode_means else None),
        "pct_low": counts[MarketState.LOW.value] / observation_count if observation_count else None,
        "pct_normal": counts[MarketState.NORMAL.value] / observation_count
        if observation_count
        else None,
        "pct_high": (
            counts[MarketState.HIGH.value] / observation_count if observation_count else None
        ),
        "pct_extreme": counts[MarketState.EXTREME.value] / observation_count
        if observation_count
        else None,
    }
    for state in states:
        state_key = state.value.lower()
        row[f"{state_key}_count"] = counts[state.value]
        row[f"{state_key}_observed_duration_seconds"] = durations[state.value]
        row.update({f"{state_key}_{key}": value for key, value in episodes[state_key].items()})
        state_items = [item for item, item_state in classified if item_state == state]
        for horizon in HORIZON_SECONDS:
            outcomes = [item.outcomes[horizon] for item in state_items if horizon in item.outcomes]
            row[f"{state_key}_{horizon}_absolute_return"] = _mean(
                outcome.absolute_return for outcome in outcomes
            )
            row[f"{state_key}_{horizon}_outcome_count"] = len(outcomes)
            row[f"{state_key}_{horizon}_realized_volatility"] = _mean(
                outcome.realized_volatility
                for outcome in outcomes
                if outcome.realized_volatility is not None
            )
            row[f"{state_key}_{horizon}_max_adverse_excursion"] = _mean(
                outcome.max_adverse_excursion
                for outcome in outcomes
                if outcome.max_adverse_excursion is not None
            )
    return row


def _regime_statistics(
    classified: Sequence[tuple[FeatureOutcomes, MarketState]],
    policy: ThresholdPolicy,
    *,
    split: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    episode_stats = _episode_statistics(classified)
    for state in (MarketState.LOW, MarketState.NORMAL, MarketState.HIGH, MarketState.EXTREME):
        state_key = state.value.lower()
        state_items = [item for item, item_state in classified if item_state == state]
        for horizon in HORIZON_SECONDS:
            outcomes = [item.outcomes[horizon] for item in state_items if horizon in item.outcomes]
            rows.append(
                {
                    "policy_name": policy.name,
                    "split": split,
                    "state": state.value,
                    "horizon": horizon,
                    "observation_count": len(state_items),
                    "outcome_count": len(outcomes),
                    "pct_state": len(state_items) / len(classified) if classified else None,
                    "absolute_return": _mean(outcome.absolute_return for outcome in outcomes),
                    "realized_volatility": _mean(
                        outcome.realized_volatility
                        for outcome in outcomes
                        if outcome.realized_volatility is not None
                    ),
                    "max_adverse_excursion": _mean(
                        outcome.max_adverse_excursion
                        for outcome in outcomes
                        if outcome.max_adverse_excursion is not None
                    ),
                    **episode_stats[state_key],
                }
            )
    return rows


_THRESHOLD_CANDIDATE_AXES: dict[str, tuple[float, ...]] = {
    "aggressive_enter_ratio": (0.75, 0.80, 0.85, 0.90),
    "aggressive_exit_ratio": (0.90, 0.95, 1.00),
    "high_enter_ratio": (1.10, 1.15, 1.20, 1.25, 1.30, 1.40),
    "high_exit_ratio": (1.05, 1.10, 1.12, 1.15),
    "extreme_enter_ratio": (1.40, 1.50, 1.60, 1.75, 2.00),
    "extreme_exit_ratio": (1.20, 1.30, 1.35, 1.40, 1.50),
}


def _threshold_candidate_axes(config: CalibrationConfig) -> dict[str, tuple[float, ...]]:
    return {
        field: tuple(sorted({*values, float(getattr(config.current_thresholds, field))}))
        for field, values in _THRESHOLD_CANDIDATE_AXES.items()
    }


def _candidate_thresholds(config: CalibrationConfig) -> tuple[ThresholdPolicy, ...]:
    policies: dict[str, ThresholdPolicy] = {}
    axes = _threshold_candidate_axes(config)
    for aggressive_enter in axes["aggressive_enter_ratio"]:
        for aggressive_exit in axes["aggressive_exit_ratio"]:
            for high_enter in axes["high_enter_ratio"]:
                for high_exit in axes["high_exit_ratio"]:
                    for extreme_enter in axes["extreme_enter_ratio"]:
                        for extreme_exit in axes["extreme_exit_ratio"]:
                            try:
                                policy = ThresholdPolicy(
                                    aggressive_enter_ratio=aggressive_enter,
                                    aggressive_exit_ratio=aggressive_exit,
                                    high_enter_ratio=high_enter,
                                    high_exit_ratio=high_exit,
                                    extreme_enter_ratio=extreme_enter,
                                    extreme_exit_ratio=extreme_exit,
                                )
                            except ValueError:
                                continue
                            policies[policy.name] = policy
    policies[config.current_thresholds.name] = config.current_thresholds
    return tuple(policies.values())


def _grid_candidates(
    config: CalibrationConfig, threshold_policy: ThresholdPolicy
) -> tuple[ResearchCandidate, ...]:
    candidates: list[ResearchCandidate] = []
    for width_policy in (WidthPolicy.STATIC_REGIME, WidthPolicy.IV_SCALED):
        # Aggressive geometry is intentionally provisional. Keep it fixed in
        # the candidate sweep until low-IV history is large enough to justify
        # calibrating another exposure/width family.
        aggressive_widths = (0.0075,)
        normal_widths = (
            config.normal_half_widths
            if width_policy == WidthPolicy.STATIC_REGIME
            else (
                config.normal_half_widths[2]
                if len(config.normal_half_widths) > 2
                else config.normal_half_widths[-1],
            )
        )
        defensive_widths = (
            config.defensive_half_widths
            if width_policy == WidthPolicy.STATIC_REGIME
            else (
                config.defensive_half_widths[2]
                if len(config.defensive_half_widths) > 2
                else config.defensive_half_widths[-1],
            )
        )
        for aggressive_width in aggressive_widths:
            for normal_width in normal_widths:
                for defensive_width in defensive_widths:
                    for normal_levels in config.normal_levels:
                        for defensive_levels in config.defensive_levels:
                            for aggressive_levels in (7,):
                                for fraction in config.defensive_quote_fractions:
                                    candidates.append(
                                        ResearchCandidate(
                                            family="GRID",
                                            threshold_policy=threshold_policy,
                                            width_policy=width_policy,
                                            normal_half_width_pct=normal_width,
                                            defensive_half_width_pct=defensive_width,
                                            width_horizon_seconds=config.width_horizon_seconds,
                                            width_sigma_multiplier=config.width_sigma_multiplier,
                                            min_half_width_pct=config.min_half_width_pct,
                                            max_half_width_pct=config.max_half_width_pct,
                                            normal_levels=normal_levels,
                                            defensive_levels=defensive_levels,
                                            defensive_quote_fraction=fraction,
                                            aggressive_half_width_pct=aggressive_width,
                                            aggressive_levels=aggressive_levels,
                                        )
                                    )
    return tuple(candidates)


def _level_size_candidates(
    config: CalibrationConfig, threshold_policy: ThresholdPolicy
) -> tuple[ResearchCandidate, ...]:
    return tuple(
        ResearchCandidate(
            family="LEVEL_SIZE",
            threshold_policy=threshold_policy,
            width_policy=WidthPolicy.STATIC_REGIME,
            normal_half_width_pct=0.010,
            defensive_half_width_pct=0.025,
            width_horizon_seconds=config.width_horizon_seconds,
            width_sigma_multiplier=config.width_sigma_multiplier,
            min_half_width_pct=config.min_half_width_pct,
            max_half_width_pct=config.max_half_width_pct,
            normal_levels=normal_levels,
            defensive_levels=defensive_levels,
            defensive_quote_fraction=fraction,
            aggressive_levels=7,
            aggressive_half_width_pct=0.0075,
        )
        for normal_levels in config.normal_levels
        for defensive_levels in config.defensive_levels
        for fraction in config.defensive_quote_fractions
    )


def _price_path(
    feature: CausalFeature,
    price_points: Sequence[PricePoint],
    *,
    horizon_seconds: float,
) -> tuple[float, ...]:
    if feature.observation.perp_mid is None:
        return ()
    start = feature.observation.decision_timestamp
    end = start + horizon_seconds
    times = [point.timestamp for point in price_points]
    first = bisect.bisect_right(times, start)
    last = bisect.bisect_right(times, end)
    return tuple(point.mid for point in price_points[first:last])


def _level_prices(center: float, half_width: float, levels: int) -> tuple[float, ...]:
    if center <= 0 or half_width <= 0 or levels < 1:
        return ()
    if levels == 1:
        return (center,)
    low = center * (1.0 - half_width)
    high = center * (1.0 + half_width)
    step = (high - low) / (levels - 1)
    return tuple(low + index * step for index in range(levels))


def _post_decision_touch_levels(center: float, half_width: float, levels: int) -> tuple[float, ...]:
    """Return levels whose touches can be attributed to future movement.

    A symmetric odd-level grid contains the decision center.  The center is
    deliberately excluded from this midpoint-touch proxy because it is not a
    directional move away from the decision price and would otherwise inflate
    touch rates.  Native executor fills are never inferred from this proxy.
    """

    return tuple(
        level
        for level in _level_prices(center, half_width, levels)
        if not math.isclose(level, center, rel_tol=1e-12, abs_tol=1e-12)
    )


def _proxy_width_metrics(
    classified: Sequence[tuple[FeatureOutcomes, MarketState]],
    policy: ThresholdPolicy,
    *,
    width_policy: WidthPolicy,
    aggressive_half_width: float = 0.0075,
    normal_half_width: float,
    defensive_half_width: float,
    aggressive_levels: int = 7,
    normal_levels: int,
    defensive_levels: int,
    defensive_quote_fraction: float,
    config: CalibrationConfig,
    price_points: Sequence[PricePoint],
    split: str,
    candidate: ResearchCandidate | None = None,
) -> dict[str, Any]:
    touched_levels = 0
    eligible_levels = 0
    samples_with_any_touch = 0
    active_samples = 0
    adverse: list[float] = []
    expected_moves: list[float] = []
    for item, state in classified:
        if state == MarketState.INITIALIZING:
            continue
        atm_iv = item.feature.observation.canonical_atm_iv()
        half_width, expected = research_half_width(
            width_policy,
            state,
            atm_iv=atm_iv,
            aggressive_half_width_pct=aggressive_half_width,
            normal_half_width_pct=normal_half_width,
            defensive_half_width_pct=defensive_half_width,
            width_horizon_seconds=config.width_horizon_seconds,
            width_sigma_multiplier=config.width_sigma_multiplier,
            min_half_width_pct=config.min_half_width_pct,
            max_half_width_pct=config.max_half_width_pct,
        )
        if half_width <= 0 or item.feature.observation.perp_mid is None:
            continue
        active_samples += 1
        if expected is not None:
            expected_moves.append(expected)
        levels = (
            aggressive_levels
            if state == MarketState.LOW
            else normal_levels
            if state == MarketState.NORMAL
            else defensive_levels
        )
        prices = _post_decision_touch_levels(item.feature.observation.perp_mid, half_width, levels)
        eligible_levels += len(prices)
        path = _price_path(
            item.feature,
            price_points,
            horizon_seconds=config.width_horizon_seconds,
        )
        if path:
            low, high = min(path), max(path)
            item_touches = sum(low <= level <= high for level in prices)
            touched_levels += item_touches
            if item_touches:
                samples_with_any_touch += 1
            if "5m" in item.outcomes and item_touches:
                outcome = item.outcomes["5m"]
                if outcome.max_adverse_excursion is not None:
                    adverse.append(outcome.max_adverse_excursion)
    state_metrics = _state_metrics(classified, policy, split=split)
    return {
        "split": split,
        "candidate_id": candidate.candidate_id if candidate is not None else None,
        "candidate_family": candidate.family if candidate is not None else "GRID",
        "evaluation_status": (
            "EVALUATED_FROM_PROXY_TOUCHES" if price_points else "CONFIGURATION_FEASIBILITY_ONLY"
        ),
        "evidence_scope": "PROXY_ONLY" if price_points else "NONE",
        "quote_fraction_identifiability": "NOT_ECONOMICALLY_IDENTIFIABLE",
        "policy_name": policy.name,
        "width_policy": width_policy.value,
        "normal_half_width_pct": normal_half_width,
        "defensive_half_width_pct": defensive_half_width,
        "aggressive_half_width_pct": aggressive_half_width,
        "aggressive_levels": aggressive_levels,
        "normal_levels": normal_levels,
        "defensive_levels": defensive_levels,
        "defensive_quote_fraction": defensive_quote_fraction,
        "width_horizon_seconds": config.width_horizon_seconds,
        "width_sigma_multiplier": config.width_sigma_multiplier,
        "min_half_width_pct": config.min_half_width_pct,
        "max_half_width_pct": config.max_half_width_pct,
        "active_sample_count": active_samples,
        "eligible_level_count": eligible_levels,
        "level_touch_count": touched_levels,
        "samples_with_any_touch": samples_with_any_touch,
        "sample_touch_rate": (samples_with_any_touch / active_samples if active_samples else None),
        "level_touch_rate": touched_levels / eligible_levels if eligible_levels else None,
        "mean_adverse_move_after_proxy_touch": _mean(adverse),
        "mean_expected_move_pct": _mean(expected_moves),
        "normal_quote": config.normal_total_quote,
        "defensive_quote": config.normal_total_quote * defensive_quote_fraction,
        "evidence_label": "SIMULATED_MID_TOUCH" if price_points else "UNAVAILABLE",
        "real_executor_fill_count": 0,
        "fees": None,
        "turnover": None,
        "net_pnl_proxy": None,
        "inventory_utilization": None,
        "drawdown": None,
        "pct_normal": state_metrics["pct_normal"],
        "pct_low": state_metrics["pct_low"],
        "pct_high": state_metrics["pct_high"],
        "pct_extreme": state_metrics["pct_extreme"],
    }


def _level_size_rows(
    config: CalibrationConfig,
    classified: Sequence[tuple[FeatureOutcomes, MarketState]],
    policy: ThresholdPolicy,
    price_points: Sequence[PricePoint],
    split: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in _level_size_candidates(config, policy):
        metric = _proxy_width_metrics(
            classified,
            policy,
            width_policy=candidate.width_policy,
            aggressive_half_width=candidate.aggressive_half_width_pct,
            normal_half_width=candidate.normal_half_width_pct,
            defensive_half_width=candidate.defensive_half_width_pct,
            aggressive_levels=candidate.aggressive_levels,
            normal_levels=candidate.normal_levels,
            defensive_levels=candidate.defensive_levels,
            defensive_quote_fraction=candidate.defensive_quote_fraction,
            config=config,
            price_points=price_points,
            split=split,
            candidate=candidate,
        )
        normal_per_level = config.normal_total_quote / candidate.normal_levels
        defensive_total = config.normal_total_quote * candidate.defensive_quote_fraction
        defensive_per_level = defensive_total / candidate.defensive_levels
        rows.append(
            {
                **metric,
                "candidate_family": "LEVEL_SIZE",
                "normal_total_quote": config.normal_total_quote,
                "defensive_total_quote": defensive_total,
                "normal_quote_per_level": normal_per_level,
                "defensive_quote_per_level": defensive_per_level,
                "minimum_order_quote_assumption": 5.0,
                "minimum_order_constraints_pass": (
                    normal_per_level >= 5.0 and defensive_per_level >= 5.0
                ),
                "quote_fraction_evidence_status": "NOT_ECONOMICALLY_IDENTIFIABLE",
                "is_baseline": (
                    candidate.normal_levels == 5
                    and candidate.defensive_levels == 3
                    and math.isclose(candidate.defensive_quote_fraction, 0.60)
                ),
            }
        )
    return rows


def _with_baseline(
    row: dict[str, Any],
    policy: ThresholdPolicy,
    baseline: ThresholdPolicy,
    config: CalibrationConfig,
) -> dict[str, Any]:
    row = dict(row)
    row["is_baseline"] = policy.name == baseline.name
    row["candidate_family"] = "THRESHOLD"
    row["candidate_id"] = ResearchCandidate(
        family="THRESHOLD",
        threshold_policy=policy,
        width_policy=WidthPolicy.STATIC_REGIME,
        normal_half_width_pct=(
            config.normal_half_widths[2]
            if len(config.normal_half_widths) > 2
            else config.normal_half_widths[-1]
        ),
        defensive_half_width_pct=(
            config.defensive_half_widths[2]
            if len(config.defensive_half_widths) > 2
            else config.defensive_half_widths[-1]
        ),
        width_horizon_seconds=config.width_horizon_seconds,
        width_sigma_multiplier=config.width_sigma_multiplier,
        min_half_width_pct=config.min_half_width_pct,
        max_half_width_pct=config.max_half_width_pct,
        normal_levels=(5 if 5 in config.normal_levels else config.normal_levels[-1]),
        defensive_levels=(3 if 3 in config.defensive_levels else config.defensive_levels[-1]),
        defensive_quote_fraction=(
            0.60
            if 0.60 in config.defensive_quote_fractions
            else config.defensive_quote_fractions[-1]
        ),
    ).candidate_id
    row["evaluation_status"] = "EVALUATED_FROM_OBSERVED_OUTCOMES"
    return row


_THRESHOLD_FIELDS = (
    "high_enter_ratio",
    "high_exit_ratio",
    "extreme_enter_ratio",
    "extreme_exit_ratio",
)


def _volatility_metrics(row: dict[str, Any]) -> dict[str, Any] | None:
    for horizon in ("1h", "30m", "15m", "5m"):
        normal = row.get(f"normal_{horizon}_realized_volatility")
        high = row.get(f"high_{horizon}_realized_volatility")
        extreme = row.get(f"extreme_{horizon}_realized_volatility")
        if normal is not None and high is not None and extreme is not None:
            return {
                "horizon": horizon,
                "normal_realized_volatility": normal,
                "high_realized_volatility": high,
                "extreme_realized_volatility": extreme,
                "high_minus_normal": high - normal,
                "extreme_minus_high": extreme - high,
            }
    return None


def _selection_score(row: dict[str, Any]) -> float | None:
    """Return a descriptive monotonic-volatility score, not a pass decision."""

    metrics = _volatility_metrics(row)
    if metrics is None:
        return None
    return float(metrics["high_minus_normal"] + metrics["extreme_minus_high"])


def _monotonic_gate(
    row: dict[str, Any], config: CalibrationConfig
) -> tuple[bool, tuple[str, ...], dict[str, Any] | None]:
    metrics = _volatility_metrics(row)
    if metrics is None:
        return False, ("realized_volatility_missing",), None
    reasons: list[str] = []
    if metrics["high_minus_normal"] <= config.monotonic_tolerance:
        reasons.append("high_not_above_normal")
    if metrics["extreme_minus_high"] < -config.monotonic_tolerance:
        reasons.append("extreme_below_high")
    return not reasons, tuple(reasons), metrics


def _selection_row_is_eligible(row: dict[str, Any], config: CalibrationConfig) -> bool:
    volatility_metrics = _volatility_metrics(row)
    if volatility_metrics is None:
        return False
    selected_horizon = volatility_metrics["horizon"]
    for state in ("normal", "high", "extreme"):
        if row.get(f"{state}_count", 0) < config.minimum_state_count:
            return False
        if row.get(f"{state}_{selected_horizon}_outcome_count", 0) < config.minimum_outcome_count:
            return False
    passes, _, _ = _monotonic_gate(row, config)
    return passes


def _configured_axis_distance(values: Sequence[float], left: float, right: float) -> int:
    axis = {float(value): index for index, value in enumerate(sorted(set(values)))}
    return abs(axis[float(left)] - axis[float(right)])


def _select_frozen_candidates(
    threshold_rows: Sequence[dict[str, Any]], config: CalibrationConfig
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Select from development/validation only; holdout is never consulted here."""

    by_policy: dict[str, dict[str, dict[str, Any]]] = {}
    for row in threshold_rows:
        by_policy.setdefault(str(row["policy_name"]), {})[str(row["split"])] = row
    eligible: dict[str, dict[str, Any]] = {}
    for policy_name, rows in by_policy.items():
        development = rows.get("development")
        validation = rows.get("validation")
        if development is None or validation is None:
            continue
        if not _selection_row_is_eligible(development, config):
            continue
        if not _selection_row_is_eligible(validation, config):
            continue
        development_score = _selection_score(development)
        validation_score = _selection_score(validation)
        if development_score is None or validation_score is None:
            continue
        development_gate, _, development_metrics = _monotonic_gate(development, config)
        validation_gate, _, validation_metrics = _monotonic_gate(validation, config)
        if (
            not development_gate
            or not validation_gate
            or development_score <= 0
            or validation_score <= 0
        ):
            continue
        eligible[policy_name] = {
            "candidate_id": development.get("candidate_id"),
            "policy_name": policy_name,
            "high_enter_ratio": development["high_enter_ratio"],
            "high_exit_ratio": development["high_exit_ratio"],
            "extreme_enter_ratio": development["extreme_enter_ratio"],
            "extreme_exit_ratio": development["extreme_exit_ratio"],
            "development_score": development_score,
            "validation_score": validation_score,
            "development_metrics": development_metrics,
            "validation_metrics": validation_metrics,
        }

    threshold_axes = _threshold_candidate_axes(config)
    stability_rows: list[dict[str, Any]] = []
    frozen: list[dict[str, Any]] = []
    for policy_name, candidate in eligible.items():
        neighbor_names: list[str] = []
        neighbor_dimensions: dict[str, list[str]] = {}
        for other_name, other in eligible.items():
            if other_name == policy_name:
                continue
            axis_distances = {
                field: abs(
                    threshold_axes[field].index(float(other[field]))
                    - threshold_axes[field].index(float(candidate[field]))
                )
                for field in _THRESHOLD_FIELDS
            }
            if all(axis_distances[field] <= 1 for field in _THRESHOLD_FIELDS):
                neighbor_names.append(other_name)
                neighbor_dimensions[other_name] = [
                    field for field in _THRESHOLD_FIELDS if axis_distances[field] > 0
                ]
        stable_neighbors = [
            other_name
            for other_name in neighbor_names
            if eligible[other_name]["development_score"] > 0
            and eligible[other_name]["validation_score"] > 0
        ]
        stability = {
            "policy_name": policy_name,
            "neighbor_count": len(neighbor_names),
            "stable_neighbor_count": len(stable_neighbors),
            "neighbor_stability": (
                len(stable_neighbors) / len(neighbor_names) if neighbor_names else None
            ),
            "neighbor_policy_names": neighbor_names,
            "neighbor_dimensions_differed": neighbor_dimensions,
            "neighbor_axis_steps": {
                other_name: {
                    field: abs(
                        threshold_axes[field].index(float(eligible[other_name][field]))
                        - threshold_axes[field].index(float(candidate[field]))
                    )
                    for field in _THRESHOLD_FIELDS
                }
                for other_name in neighbor_names
            },
        }
        stability_rows.append(stability)
        if len(stable_neighbors) >= config.minimum_neighbor_count:
            frozen.append({**candidate, **stability})

    frozen.sort(key=lambda row: float(row["validation_score"]), reverse=True)
    if not frozen:
        return "NO_ROBUST_CANDIDATE", (), tuple(stability_rows)
    return (
        "FROZEN_FOR_HOLDOUT",
        tuple(frozen[: config.max_frozen_candidates]),
        tuple(stability_rows),
    )


_GRID_FIELDS = (
    "normal_half_width_pct",
    "defensive_half_width_pct",
    "normal_levels",
    "defensive_levels",
    "defensive_quote_fraction",
)


def _grid_candidate_is_eligible(row: dict[str, Any], config: CalibrationConfig) -> bool:
    return (
        row.get("active_sample_count", 0) >= config.minimum_proxy_sample_count
        and row.get("sample_touch_rate") is not None
        and row.get("level_touch_rate") is not None
    )


def _select_proxy_grid_candidates(
    width_rows: Sequence[dict[str, Any]], config: CalibrationConfig
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Find proxy-stable grid regions without selecting an economic quote fraction."""

    by_candidate: dict[str, dict[str, dict[str, Any]]] = {}
    for row in width_rows:
        candidate_id = row.get("candidate_id")
        if candidate_id is None:
            continue
        by_candidate.setdefault(str(candidate_id), {})[str(row["split"])] = row
    eligible: dict[str, dict[str, Any]] = {}
    for candidate_id, rows in by_candidate.items():
        development = rows.get("development")
        validation = rows.get("validation")
        if development is None or validation is None:
            continue
        if not _grid_candidate_is_eligible(development, config):
            continue
        if not _grid_candidate_is_eligible(validation, config):
            continue
        eligible[candidate_id] = {
            "candidate_id": candidate_id,
            "candidate_family": development.get("candidate_family", "GRID"),
            "policy_name": development["policy_name"],
            "width_policy": development["width_policy"],
            **{field: development[field] for field in _GRID_FIELDS},
            "development_sample_touch_rate": development["sample_touch_rate"],
            "validation_sample_touch_rate": validation["sample_touch_rate"],
            "development_level_touch_rate": development["level_touch_rate"],
            "validation_level_touch_rate": validation["level_touch_rate"],
            "quote_fraction_identifiability": "NOT_ECONOMICALLY_IDENTIFIABLE",
        }
    if not eligible:
        return "INSUFFICIENT_DATA", (), ()

    configured_grid_axes = {
        "normal_half_width_pct": config.normal_half_widths,
        "defensive_half_width_pct": config.defensive_half_widths,
        "normal_levels": tuple(float(value) for value in config.normal_levels),
        "defensive_levels": tuple(float(value) for value in config.defensive_levels),
        "defensive_quote_fraction": config.defensive_quote_fractions,
    }
    grid_axes = {
        field: tuple(
            sorted(
                {
                    *map(float, values),
                    *(float(row[field]) for row in eligible.values()),
                }
            )
        )
        for field, values in configured_grid_axes.items()
    }
    stability_rows: list[dict[str, Any]] = []
    frozen: list[dict[str, Any]] = []
    for candidate_id, candidate in eligible.items():
        neighbor_names: list[str] = []
        neighbor_dimensions: dict[str, list[str]] = {}
        for other_id, other in eligible.items():
            if other_id == candidate_id or other["width_policy"] != candidate["width_policy"]:
                continue
            axis_distances = {
                field: _configured_axis_distance(
                    grid_axes[field], float(other[field]), float(candidate[field])
                )
                for field in _GRID_FIELDS
            }
            if all(axis_distances[field] <= 1 for field in _GRID_FIELDS):
                neighbor_names.append(other_id)
                neighbor_dimensions[other_id] = [
                    field for field in _GRID_FIELDS if axis_distances[field] > 0
                ]
        stable_neighbors = [
            other_id
            for other_id in neighbor_names
            if abs(
                eligible[other_id]["development_sample_touch_rate"]
                - candidate["development_sample_touch_rate"]
            )
            <= config.proxy_stability_tolerance
            and abs(
                eligible[other_id]["validation_sample_touch_rate"]
                - candidate["validation_sample_touch_rate"]
            )
            <= config.proxy_stability_tolerance
        ]
        stability = {
            "candidate_id": candidate_id,
            "selection_role": "PROXY_STABLE_ONLY",
            "neighbor_count": len(neighbor_names),
            "stable_neighbor_count": len(stable_neighbors),
            "neighbor_stability": (
                len(stable_neighbors) / len(neighbor_names) if neighbor_names else None
            ),
            "neighbor_candidate_ids": neighbor_names,
            "neighbor_dimensions_differed": neighbor_dimensions,
            "neighbor_axis_steps": {
                other_id: {
                    field: _configured_axis_distance(
                        grid_axes[field],
                        float(eligible[other_id][field]),
                        float(candidate[field]),
                    )
                    for field in _GRID_FIELDS
                }
                for other_id in neighbor_names
            },
            "evidence_scope": "PROXY_ONLY",
        }
        stability_rows.append(stability)
        if len(stable_neighbors) >= config.minimum_neighbor_count:
            frozen.append({**candidate, **stability})

    frozen.sort(key=lambda row: float(row["stable_neighbor_count"]), reverse=True)
    if not frozen:
        return "NO_ROBUST_CANDIDATE", (), tuple(stability_rows)
    return (
        "NOT_ECONOMICALLY_IDENTIFIABLE",
        tuple(frozen[: config.max_frozen_candidates]),
        tuple(stability_rows),
    )


def _holdout_evaluation(
    candidate: dict[str, Any] | None,
    holdout_row: dict[str, Any] | None,
    config: CalibrationConfig,
) -> dict[str, Any]:
    """Apply the predeclared gate after candidate freezing, without retuning."""

    if holdout_row is None:
        return {
            "candidate_id": candidate.get("candidate_id") if candidate else None,
            "holdout_pass": False,
            "holdout_failure_reasons": ("holdout_row_missing",),
            "holdout_gate": "HIGH_RV_ABOVE_NORMAL_AND_EXTREME_RV_NOT_BELOW_HIGH_RV",
        }
    passes, reasons, metrics = _monotonic_gate(holdout_row, config)
    if not _selection_row_is_eligible(holdout_row, config):
        reasons = (*reasons, "holdout_state_or_outcome_counts_below_minimum")
        passes = False
    return {
        "holdout_pass": passes,
        "holdout_failure_reasons": tuple(dict.fromkeys(reasons)),
        "holdout_metrics": metrics,
        "holdout_score": _selection_score(holdout_row),
        "candidate_id": candidate.get("candidate_id") if candidate else None,
        "holdout_gate": "HIGH_RV_ABOVE_NORMAL_AND_EXTREME_RV_NOT_BELOW_HIGH_RV",
    }


def calibrate(
    observations: Sequence[CalibrationObservation],
    *,
    price_points: Sequence[PricePoint] | None = None,
    config: CalibrationConfig | None = None,
    source_path: str | None = None,
) -> CalibrationReport:
    """Run all offline experiments without accessing live or private APIs."""

    config = CalibrationConfig() if config is None else config
    price_points = tuple(price_points or ())
    feature_build = build_causal_features(
        observations,
        min_history=config.min_history,
        max_history=config.max_history,
        max_source_age_seconds=config.max_source_age_seconds,
        source_path=source_path,
    )
    # Derive a price series from the observation stream only when the caller
    # did not provide one.  This is still causal: these points are used only
    # as future labels by attach_forward_outcomes.
    if not price_points:
        price_points = tuple(
            PricePoint(
                timestamp=row.decision_timestamp,
                mid=row.perp_mid,
                best_bid=row.best_bid,
                best_ask=row.best_ask,
            )
            for row in feature_build.accepted_observations
            if row.perp_mid is not None
        )
    outcomes = attach_forward_outcomes(feature_build.features, price_points)
    development, validation, holdout = chronological_split(
        outcomes,
        development_fraction=config.development_fraction,
        validation_fraction=config.validation_fraction,
    )
    split_map = {"development": development, "validation": validation, "holdout": holdout}
    limitations: list[str] = []
    blocking_limitations: list[str] = []
    if not observations:
        limitation = "no observation file was supplied"
        limitations.append(limitation)
        blocking_limitations.append(limitation)
    if feature_build.audit.rejected_rows:
        limitations.append("some rows were rejected by the causal/schema audit")
    if len(feature_build.features) < config.minimum_feature_count:
        limitation = (
            f"only {len(feature_build.features)} causal features; "
            f"at least {config.minimum_feature_count} are required for calibration"
        )
        limitations.append(limitation)
        blocking_limitations.append(limitation)
    if len(holdout) < config.minimum_holdout_count:
        limitation = (
            f"only {len(holdout)} chronological holdout features; "
            f"at least {config.minimum_holdout_count} are required"
        )
        limitations.append(limitation)
        blocking_limitations.append(limitation)
    if not price_points:
        limitation = "no perpetual price series is available for forward outcomes"
        limitations.append(limitation)
        blocking_limitations.append(limitation)
    limitations.append("historical order-book depth and fee/fill records are not available")
    recommendation = "KEEP_CURRENT_DEFAULTS"

    threshold_rows: list[dict[str, Any]] = []
    regime_rows: list[dict[str, Any]] = []
    if feature_build.features:
        for policy in _candidate_thresholds(config):
            classified = classify_features(outcomes, policy)
            classified_splits = dict(
                zip(
                    ("development", "validation", "holdout"),
                    chronological_split(
                        classified,
                        development_fraction=config.development_fraction,
                        validation_fraction=config.validation_fraction,
                    ),
                    strict=True,
                )
            )
            for split, items in classified_splits.items():
                row = _state_metrics(items, policy, split=split)
                threshold_rows.append(
                    _with_baseline(row, policy, config.current_thresholds, config)
                )
                regime_rows.extend(_regime_statistics(items, policy, split=split))

    current_classified = classify_features(outcomes, config.current_thresholds)
    current_classified_splits = dict(
        zip(
            ("development", "validation", "holdout"),
            chronological_split(
                current_classified,
                development_fraction=config.development_fraction,
                validation_fraction=config.validation_fraction,
            ),
            strict=True,
        )
    )

    width_rows: list[dict[str, Any]] = []
    if feature_build.features:
        grid_candidates = _grid_candidates(config, config.current_thresholds)
        for split, items in current_classified_splits.items():
            for candidate in grid_candidates:
                width_rows.append(
                    _proxy_width_metrics(
                        items,
                        config.current_thresholds,
                        width_policy=candidate.width_policy,
                        aggressive_half_width=candidate.aggressive_half_width_pct,
                        normal_half_width=candidate.normal_half_width_pct,
                        defensive_half_width=candidate.defensive_half_width_pct,
                        aggressive_levels=candidate.aggressive_levels,
                        normal_levels=candidate.normal_levels,
                        defensive_levels=candidate.defensive_levels,
                        defensive_quote_fraction=candidate.defensive_quote_fraction,
                        config=config,
                        price_points=price_points,
                        split=split,
                        candidate=candidate,
                    )
                )

    level_rows: list[dict[str, Any]] = []
    for split, items in current_classified_splits.items():
        level_rows.extend(
            _level_size_rows(
                config,
                items,
                config.current_thresholds,
                price_points,
                split,
            )
        )

    current_rows = [
        row
        for row in threshold_rows
        if row.get("is_baseline") and row.get("split") in {"development", "validation", "holdout"}
    ]
    current_default_metrics = {
        "available": bool(current_rows),
        "threshold": current_rows,
        "width": [
            row
            for row in width_rows
            if row["width_policy"] == WidthPolicy.STATIC_REGIME.value
            and math.isclose(row["normal_half_width_pct"], 0.010)
            and math.isclose(row["defensive_half_width_pct"], 0.025)
            and row["normal_levels"] == 5
            and row["defensive_levels"] == 3
            and math.isclose(row["defensive_quote_fraction"], 0.60)
        ],
    }
    candidate_selection_status, frozen_candidates, neighbor_stability = _select_frozen_candidates(
        threshold_rows, config
    )
    (
        grid_candidate_selection_status,
        proxy_grid_candidates,
        grid_neighbor_stability,
    ) = _select_proxy_grid_candidates(width_rows, config)
    frozen_grid_candidates: tuple[dict[str, Any], ...] = ()
    frozen_holdout_rows: list[dict[str, Any]] = []
    if blocking_limitations:
        candidate_selection_status = "INSUFFICIENT_DATA"
        frozen_candidates = ()
        grid_candidate_selection_status = "INSUFFICIENT_DATA"
        frozen_grid_candidates = ()
        status = "INSUFFICIENT_DATA"
    elif candidate_selection_status != "FROZEN_FOR_HOLDOUT":
        status = "NO_ROBUST_CANDIDATE"
        limitations.append(
            "no threshold candidate met the per-state outcome and neighbor-stability gates"
        )
    else:
        threshold_holdout_passes = 0
        for candidate in frozen_candidates:
            holdout_row = next(
                (
                    row
                    for row in threshold_rows
                    if row.get("split") == "holdout"
                    and row.get("policy_name") == candidate["policy_name"]
                ),
                None,
            )
            evaluation = _holdout_evaluation(candidate, holdout_row, config)
            if evaluation["holdout_pass"]:
                threshold_holdout_passes += 1
            if holdout_row is not None:
                frozen_holdout_rows.append(
                    {
                        "model": "frozen_candidate",
                        **holdout_row,
                        **evaluation,
                    }
                )
        if threshold_holdout_passes:
            candidate_selection_status = "CANDIDATE_FOR_SHADOW_VALIDATION"
            status = "CANDIDATE_FOR_SHADOW_VALIDATION"
        else:
            candidate_selection_status = "HOLDOUT_REJECTED"
            status = "HOLDOUT_REJECTED"
            limitations.append(
                "all frozen threshold candidates failed the predeclared holdout monotonicity gate"
            )
    holdout_rows = [
        {"model": "current_defaults", **row}
        for row in current_rows
        if row.get("split") == "holdout"
    ]
    holdout_rows.extend(frozen_holdout_rows)
    if grid_candidate_selection_status == "NOT_ECONOMICALLY_IDENTIFIABLE":
        limitations.append(
            "grid candidates contain proxy-stable regions only; no quote fraction is "
            "selected because sizing is not economically identifiable without fills, "
            "fees, and inventory evidence"
        )
    split_counts = {name: len(items) for name, items in split_map.items()}
    return CalibrationReport(
        status=status,
        recommendation=recommendation,
        audit=feature_build.audit,
        feature_count=len(feature_build.features),
        split_counts=split_counts,
        data_limitations=tuple(dict.fromkeys(limitations)),
        candidate_selection_status=candidate_selection_status,
        frozen_candidates=frozen_candidates,
        neighbor_stability=neighbor_stability,
        grid_candidate_selection_status=grid_candidate_selection_status,
        frozen_grid_candidates=frozen_grid_candidates,
        proxy_grid_candidates=proxy_grid_candidates,
        grid_neighbor_stability=grid_neighbor_stability,
        current_default_metrics=current_default_metrics,
        regime_statistics=tuple(regime_rows),
        state_threshold_results=tuple(threshold_rows),
        width_results=tuple(width_rows),
        level_size_results=tuple(level_rows),
        holdout_results=tuple(holdout_rows),
    )


def report_json(report: CalibrationReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
