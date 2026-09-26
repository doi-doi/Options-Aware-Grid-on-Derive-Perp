#!/usr/bin/env python3
"""Run the offline causal calibration/replay pipeline.

This command reads only saved public/shadow observations. It never imports a
connector client, private API, bot manager, or order placement surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if TYPE_CHECKING:
    from derive_options_adaptive_grid.research.models import PricePoint


def _load_prices(path: Path) -> list[PricePoint]:
    import csv

    from derive_options_adaptive_grid.research.models import PricePoint

    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [PricePoint.from_mapping(row) for row in csv.DictReader(handle)]
    rows = []
    with path.open(encoding="utf-8") as handle:
        if path.suffix.lower() == ".json":
            payload = json.load(handle)
            rows = payload if isinstance(payload, list) else payload.get("prices", [])
        else:
            rows = [json.loads(line) for line in handle if line.strip()]
    return [PricePoint.from_mapping(row) for row in rows]


def main() -> int:
    from derive_options_adaptive_grid.research.calibration import calibrate
    from derive_options_adaptive_grid.research.io import load_observations
    from derive_options_adaptive_grid.research.reporting import write_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/calibration/observations.jsonl"),
        help="JSONL/JSON/CSV causal shadow observations",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=None,
        help="Optional JSONL/JSON/CSV perpetual price series",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/calibration"),
        help="Directory for machine-readable and Markdown reports",
    )
    args = parser.parse_args()
    observations = load_observations(args.input) if args.input.exists() else []
    prices = _load_prices(args.prices) if args.prices and args.prices.exists() else None
    report = calibrate(
        observations,
        price_points=prices,
        source_path=str(args.input),
    )
    paths = write_report(report, args.output_dir)
    print(
        json.dumps(
            {
                "status": report.status,
                "recommendation": report.recommendation,
                "feature_count": report.feature_count,
                "output_files": [str(path) for path in paths],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
