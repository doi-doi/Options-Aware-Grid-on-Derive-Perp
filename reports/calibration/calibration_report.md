# Derive SOL options-IV grid calibration

- Status: **INSUFFICIENT_DATA**
- Recommendation: **KEEP_CURRENT_DEFAULTS**
- Causal features: 0
- Split counts: `{"development": 0, "holdout": 0, "validation": 0}`
- Candidate selection: **INSUFFICIENT_DATA**
- Grid candidate selection: **INSUFFICIENT_DATA**

## Current production/shadow defaults

The checked-in baseline remains high enter 1.25, high exit 1.12, extreme enter 1.60, extreme exit 1.35, NORMAL ±1.0% / 5 levels / 100 quote, and DEFENSIVE ±2.5% / 3 levels / 60% quote. No live default is changed by this report.

## Current-default metrics

```json
{
  "available": false,
  "threshold": [],
  "width": []
}
```

## Candidate and holdout results

- Threshold result rows: 0
- Width result rows: 0
- Level/size result rows: 135
- Holdout result rows: 0
- Frozen candidate rows: 0
- Proxy-stable grid rows: 0

Touch counts and rates are labelled `SIMULATED_MID_TOUCH`; they use strictly post-decision midpoint paths and exclude the symmetric grid center. They are not native executor fills. Real executor fill count is zero unless separately proven by native order evidence, which this offline input contract does not fabricate.

## Data limitations

- no observation file was supplied
- only 0 causal features; at least 30 are required for calibration
- only 0 chronological holdout features; at least 5 are required
- no perpetual price series is available for forward outcomes
- historical order-book depth and fee/fill records are not available

## Frozen candidate selection

```json
{
  "frozen_candidates": [],
  "frozen_grid_candidates": [],
  "grid_neighbor_stability": [],
  "grid_status": "INSUFFICIENT_DATA",
  "neighbor_stability": [],
  "proxy_grid_candidates": [],
  "status": "INSUFFICIENT_DATA"
}
```

## Decision

The current repository does not automatically promote a candidate. A changed parameter set requires compatible causal history, chronological holdout evidence, and stable neighboring-parameter results.
