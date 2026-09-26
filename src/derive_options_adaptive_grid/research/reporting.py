"""Machine-readable and human-readable calibration report writers."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .calibration import CalibrationReport

REPORT_FILES = (
    "data_audit.json",
    "state_threshold_results.csv",
    "width_results.csv",
    "level_size_results.csv",
    "regime_statistics.csv",
    "holdout_results.csv",
    "calibration_report.json",
    "calibration_report.md",
)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return value


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["status"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def markdown_report(report: CalibrationReport) -> str:
    lines = [
        "# Derive SOL options-IV grid calibration",
        "",
        f"- Status: **{report.status}**",
        f"- Recommendation: **{report.recommendation}**",
        f"- Causal features: {report.feature_count}",
        f"- Split counts: `{json.dumps(report.split_counts, sort_keys=True)}`",
        f"- Candidate selection: **{report.candidate_selection_status}**",
        f"- Grid candidate selection: **{report.grid_candidate_selection_status}**",
        "",
        "## Current production/shadow defaults",
        "",
        "The checked-in baseline remains high enter 1.25, high exit 1.12, "
        "extreme enter 1.60, extreme exit 1.35, NORMAL ±1.0% / 5 levels / 100 quote, "
        "and DEFENSIVE ±2.5% / 3 levels / 60% quote. No live default is changed by this report.",
        "",
        "## Current-default metrics",
        "",
        "```json",
        json.dumps(report.current_default_metrics, indent=2, sort_keys=True),
        "```",
        "",
        "## Candidate and holdout results",
        "",
        f"- Threshold result rows: {len(report.state_threshold_results)}",
        f"- Width result rows: {len(report.width_results)}",
        f"- Level/size result rows: {len(report.level_size_results)}",
        f"- Holdout result rows: {len(report.holdout_results)}",
        f"- Frozen candidate rows: {len(report.frozen_candidates)}",
        f"- Proxy-stable grid rows: {len(report.proxy_grid_candidates)}",
        "",
        "Touch counts and rates are labelled `SIMULATED_MID_TOUCH`; they use "
        "strictly post-decision midpoint paths and exclude the symmetric grid center. "
        "They are not native executor fills. Real executor fill count is zero unless "
        "separately proven by native order evidence, which this offline input contract "
        "does not fabricate.",
        "",
        "## Data limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report.data_limitations or ("None reported",))
    lines.extend(
        [
            "",
            "## Frozen candidate selection",
            "",
            "```json",
            json.dumps(
                {
                    "status": report.candidate_selection_status,
                    "frozen_candidates": report.frozen_candidates,
                    "neighbor_stability": report.neighbor_stability,
                    "grid_status": report.grid_candidate_selection_status,
                    "frozen_grid_candidates": report.frozen_grid_candidates,
                    "proxy_grid_candidates": report.proxy_grid_candidates,
                    "grid_neighbor_stability": report.grid_neighbor_stability,
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            "The current repository does not automatically promote a candidate. "
            "A changed parameter set requires compatible causal history, chronological "
            "holdout evidence, and stable neighboring-parameter results.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: CalibrationReport, output_dir: str | Path) -> tuple[Path, ...]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data_audit.json").write_text(
        json.dumps(report.audit.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "state_threshold_results.csv", report.state_threshold_results)
    _write_csv(output_dir / "width_results.csv", report.width_results)
    _write_csv(output_dir / "level_size_results.csv", report.level_size_results)
    _write_csv(output_dir / "regime_statistics.csv", report.regime_statistics)
    _write_csv(output_dir / "holdout_results.csv", report.holdout_results)
    (output_dir / "calibration_report.json").write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "calibration_report.md").write_text(markdown_report(report), encoding="utf-8")
    return tuple(output_dir / name for name in REPORT_FILES)
