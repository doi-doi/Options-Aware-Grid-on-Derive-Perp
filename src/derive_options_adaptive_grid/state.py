"""Causal IV history and hysteretic market-state classification."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

from .models import MarketState, OptionsSnapshot, StateDecision


@dataclass(frozen=True, slots=True)
class StateConfig:
    min_history: int = 5
    max_history: int = 120
    aggressive_enter_ratio: float = 0.85
    aggressive_exit_ratio: float = 0.95
    high_enter_ratio: float = 1.25
    high_exit_ratio: float = 1.12
    extreme_enter_ratio: float = 1.60
    extreme_exit_ratio: float = 1.35
    max_option_age_seconds: float = 15.0
    future_tolerance_seconds: float = 2.0

    def __post_init__(self) -> None:
        """Reject policies that cannot express monotone hysteresis.

        ``extreme_exit_ratio >= high_enter_ratio`` intentionally leaves a
        band in which an EXTREME state can fall back to HIGH without first
        becoming NORMAL.  That prevents a sharp but incomplete IV retrace
        from losing the defensive mode too early.
        """

        ratios = {
            "aggressive_enter_ratio": self.aggressive_enter_ratio,
            "aggressive_exit_ratio": self.aggressive_exit_ratio,
            "high_enter_ratio": self.high_enter_ratio,
            "high_exit_ratio": self.high_exit_ratio,
            "extreme_enter_ratio": self.extreme_enter_ratio,
            "extreme_exit_ratio": self.extreme_exit_ratio,
        }
        if any(not math.isfinite(value) for value in ratios.values()):
            raise ValueError("state ratios must be finite")
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
                "state ratios must satisfy "
                "0 < aggressive_enter_ratio < aggressive_exit_ratio <= 1.0 <= "
                "high_exit_ratio < high_enter_ratio <= "
                "extreme_exit_ratio < extreme_enter_ratio"
            )
        if self.min_history < 1:
            raise ValueError("min_history must be at least 1")
        if self.max_history < self.min_history:
            raise ValueError("max_history must be at least min_history")


@dataclass
class CausalIVState:
    """Accepts each source timestamp once and never forward-fills a missing IV."""

    config: StateConfig = field(default_factory=StateConfig)
    _observations: list[tuple[float, float, float]] = field(default_factory=list, init=False)
    _last_source_timestamp: float | None = field(default=None, init=False)
    _last_state: MarketState = field(default=MarketState.INITIALIZING, init=False)

    @property
    def observations(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(self._observations)

    def observe(self, snapshot: OptionsSnapshot, *, decision_timestamp: float) -> StateDecision:
        reasons: list[str] = []
        if snapshot.underlying != "SOL":
            reasons.append("options_underlying_must_be_SOL")
        if not snapshot.option_data_available or snapshot.atm_iv is None:
            reasons.extend(snapshot.errors or ("options_data_unavailable",))
        if snapshot.source_timestamp is None or snapshot.received_timestamp is None:
            reasons.append("options_timestamp_missing")
        else:
            if (
                snapshot.source_timestamp
                > decision_timestamp + self.config.future_tolerance_seconds
            ):
                reasons.append("options_source_timestamp_in_future")
            if (
                snapshot.received_timestamp
                > decision_timestamp + self.config.future_tolerance_seconds
            ):
                reasons.append("options_receipt_timestamp_in_future")
            if (
                snapshot.source_timestamp
                > snapshot.received_timestamp + self.config.future_tolerance_seconds
            ):
                reasons.append("options_source_after_receipt")
            if decision_timestamp - snapshot.source_timestamp > self.config.max_option_age_seconds:
                reasons.append("options_observation_stale")
            if (
                self._last_source_timestamp is not None
                and snapshot.source_timestamp <= self._last_source_timestamp
            ):
                reasons.append("options_observation_out_of_order")
        if reasons:
            return StateDecision(
                state=MarketState.INITIALIZING,
                current_iv=None,
                baseline_iv=self._baseline(),
                iv_ratio=None,
                history_size=len(self._observations),
                accepted=False,
                decision_timestamp=decision_timestamp,
                reasons=tuple(dict.fromkeys(reasons)),
            )

        assert snapshot.atm_iv is not None
        assert snapshot.source_timestamp is not None
        assert snapshot.received_timestamp is not None
        baseline = self._baseline()
        self._observations.append(
            (snapshot.source_timestamp, snapshot.received_timestamp, snapshot.atm_iv)
        )
        self._last_source_timestamp = snapshot.source_timestamp
        if len(self._observations) > self.config.max_history:
            self._observations = self._observations[-self.config.max_history :]
        if baseline is None:
            state = MarketState.INITIALIZING
            ratio = None
            reasons.append(f"warming_up:{len(self._observations)}/{self.config.min_history}")
        else:
            ratio = snapshot.atm_iv / baseline if baseline > 0 else None
            if ratio is None:
                state = MarketState.INITIALIZING
                reasons.append("invalid_iv_baseline")
            elif self._last_state == MarketState.EXTREME and ratio > self.config.extreme_exit_ratio:
                state = MarketState.EXTREME
                reasons.append("extreme_iv_hysteresis")
            elif ratio >= self.config.extreme_enter_ratio:
                state = MarketState.EXTREME
                reasons.append("iv_ratio_extreme")
            elif self._last_state == MarketState.HIGH and ratio > self.config.high_exit_ratio:
                state = MarketState.HIGH
                reasons.append("high_iv_hysteresis")
            elif ratio >= self.config.high_enter_ratio:
                state = MarketState.HIGH
                reasons.append("iv_ratio_high")
            elif self._last_state == MarketState.LOW and ratio < self.config.aggressive_exit_ratio:
                state = MarketState.LOW
                reasons.append("low_iv_hysteresis")
            elif ratio <= self.config.aggressive_enter_ratio:
                state = MarketState.LOW
                reasons.append("iv_ratio_low")
            else:
                state = MarketState.NORMAL
                reasons.append("iv_ratio_normal")
        if state != MarketState.INITIALIZING:
            self._last_state = state
        return StateDecision(
            state=state,
            current_iv=snapshot.atm_iv,
            baseline_iv=baseline,
            iv_ratio=ratio,
            history_size=len(self._observations),
            accepted=True,
            decision_timestamp=decision_timestamp,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _baseline(self) -> float | None:
        # ``min_history`` counts the current observation.  The baseline is
        # still calculated only from observations that arrived before it, so
        # the first classified point has min_history - 1 prior points and can
        # never influence its own baseline.
        minimum_prior = max(1, self.config.min_history - 1)
        if len(self._observations) < minimum_prior:
            return None
        return float(median(item[2] for item in self._observations[-self.config.max_history :]))
