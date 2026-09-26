"""Causal feature construction and future-only outcome labelling."""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import TypeVar

from .io import ObservationAudit, audit_observations
from .models import CalibrationObservation, PricePoint

YEAR_SECONDS = 365.0 * 24.0 * 60.0 * 60.0
HORIZON_SECONDS: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class CausalFeature:
    observation: CalibrationObservation
    prior_median_iv: float
    iv_ratio: float


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    horizon: str
    target_timestamp: float
    signed_return: float
    absolute_return: float
    high_low_excursion: float | None
    realized_volatility: float | None
    max_favorable_excursion: float | None
    max_adverse_excursion: float | None


@dataclass(frozen=True, slots=True)
class FeatureBuildResult:
    features: tuple[CausalFeature, ...]
    accepted_observations: tuple[CalibrationObservation, ...]
    audit: ObservationAudit


@dataclass(frozen=True, slots=True)
class FeatureOutcomes:
    feature: CausalFeature
    outcomes: dict[str, ForwardOutcome]


T = TypeVar("T")


def chronological_split(
    items: Sequence[T], *, development_fraction: float = 0.60, validation_fraction: float = 0.20
) -> tuple[tuple[T, ...], tuple[T, ...], tuple[T, ...]]:
    """Split in input order; never shuffle timestamped observations."""

    if not 0 < development_fraction < 1:
        raise ValueError("development_fraction must be between 0 and 1")
    if not 0 <= validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    if development_fraction + validation_fraction >= 1:
        raise ValueError("development and validation fractions must leave a holdout")
    size = len(items)
    development_end = int(size * development_fraction)
    validation_end = int(size * (development_fraction + validation_fraction))
    if size >= 3:
        development_end = max(1, min(development_end, size - 2))
        validation_end = max(development_end + 1, min(validation_end, size - 1))
    return (
        tuple(items[:development_end]),
        tuple(items[development_end:validation_end]),
        tuple(items[validation_end:]),
    )


def build_causal_features(
    observations: Sequence[CalibrationObservation],
    *,
    min_history: int = 5,
    max_history: int = 120,
    max_source_age_seconds: float | None = 15.0,
    source_path: str | None = None,
) -> FeatureBuildResult:
    """Replay accepted IV observations and exclude the current row from its baseline."""

    if min_history < 1 or max_history < min_history:
        raise ValueError("history must satisfy 1 <= min_history <= max_history")
    accepted, audit = audit_observations(
        observations,
        source_path=source_path,
        max_source_age_seconds=max_source_age_seconds,
    )
    history: list[float] = []
    features: list[CausalFeature] = []
    for observation in accepted:
        current_iv = observation.canonical_atm_iv()
        if len(history) < max(1, min_history - 1):
            history.append(current_iv)
            history = history[-max_history:]
            continue
        baseline = float(median(history[-max_history:]))
        if baseline <= 0:
            history.append(current_iv)
            history = history[-max_history:]
            continue
        features.append(
            CausalFeature(
                observation=observation,
                prior_median_iv=baseline,
                iv_ratio=current_iv / baseline,
            )
        )
        history.append(current_iv)
        history = history[-max_history:]
    return FeatureBuildResult(
        features=tuple(features),
        accepted_observations=tuple(accepted),
        audit=audit,
    )


def _validate_price_points(price_points: Sequence[PricePoint]) -> tuple[PricePoint, ...]:
    previous_timestamp: float | None = None
    for point in price_points:
        if point.timestamp <= 0 or point.mid <= 0:
            raise ValueError("price points must have positive timestamp and mid")
        if previous_timestamp is not None and point.timestamp <= previous_timestamp:
            raise ValueError("price points must be strictly chronological")
        previous_timestamp = point.timestamp
    return tuple(price_points)


def _point_extreme(point: PricePoint, *, high: bool) -> float:
    value = point.high if high else point.low
    return value if value is not None and value > 0 else point.mid


def _realized_volatility(prices: Sequence[float], duration_seconds: float) -> float | None:
    """Annualized realized volatility from all observed log-return variance."""

    if len(prices) < 2 or duration_seconds <= 0:
        return None
    returns = [
        math.log(current / previous) for previous, current in zip(prices, prices[1:], strict=False)
    ]
    if not returns:
        return None
    return math.sqrt(sum(item * item for item in returns) * YEAR_SECONDS / duration_seconds)


def attach_forward_outcomes(
    features: Sequence[CausalFeature],
    price_points: Sequence[PricePoint],
    *,
    horizons: dict[str, int] | None = None,
) -> tuple[FeatureOutcomes, ...]:
    """Attach only prices at or after each requested future horizon."""

    points = _validate_price_points(price_points)
    horizons = HORIZON_SECONDS if horizons is None else horizons
    times = [point.timestamp for point in points]
    result: list[FeatureOutcomes] = []
    for feature in features:
        start = feature.observation.perp_mid
        if start is None or start <= 0:
            result.append(FeatureOutcomes(feature, {}))
            continue
        outcomes: dict[str, ForwardOutcome] = {}
        for name, seconds in horizons.items():
            target = feature.observation.decision_timestamp + seconds
            end_index = bisect.bisect_left(times, target)
            if end_index >= len(points):
                continue
            start_index = bisect.bisect_left(times, feature.observation.decision_timestamp)
            window = points[start_index : end_index + 1]
            if not window:
                continue
            endpoint = points[end_index]
            signed_return = endpoint.mid / start - 1.0
            highs = [_point_extreme(point, high=True) for point in window]
            lows = [_point_extreme(point, high=False) for point in window]
            high_low = (max(highs) - min(lows)) / start
            future_prices = [
                point.mid
                for point in window
                if point.timestamp > feature.observation.decision_timestamp
            ]
            price_path = [start, *future_prices]
            outcomes[name] = ForwardOutcome(
                horizon=name,
                target_timestamp=endpoint.timestamp,
                signed_return=signed_return,
                absolute_return=abs(signed_return),
                high_low_excursion=high_low,
                realized_volatility=_realized_volatility(
                    price_path, endpoint.timestamp - feature.observation.decision_timestamp
                ),
                max_favorable_excursion=max(0.0, max(highs) / start - 1.0),
                max_adverse_excursion=max(0.0, 1.0 - min(lows) / start),
            )
        result.append(FeatureOutcomes(feature, outcomes))
    return tuple(result)
