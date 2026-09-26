"""Offline, causal calibration tools for the SOL options-IV grid.

The research package is deliberately independent of Hummingbot and exchange
clients.  It consumes saved observations and labels simulated evidence as
such; it never creates an order or treats a price touch as a real fill.
"""

from .calibration import CalibrationConfig, CalibrationReport, calibrate
from .features import HORIZON_SECONDS, build_causal_features, chronological_split
from .models import CalibrationObservation, PricePoint
from .policies import (
    WidthPolicy,
    expected_move_pct,
    iv_scaled_half_width,
    static_half_width,
)

__all__ = [
    "CalibrationConfig",
    "CalibrationObservation",
    "CalibrationReport",
    "HORIZON_SECONDS",
    "PricePoint",
    "WidthPolicy",
    "build_causal_features",
    "calibrate",
    "chronological_split",
    "expected_move_pct",
    "iv_scaled_half_width",
    "static_half_width",
]
