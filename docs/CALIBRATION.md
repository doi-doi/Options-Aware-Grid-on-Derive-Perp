# Causal calibration workflow

The checked-in strategy numbers are shadow baselines, not empirically proven
optima. The research package under `src/derive_options_adaptive_grid/research`
replays saved observations in decision-time order and keeps current IV out of
its own rolling median baseline.

## Input boundary

The input contract is documented in
[`data/calibration/README.md`](../data/calibration/README.md). Every accepted
row must identify the SOL perpetual, a same-strike SOL call/put pair, and
source, receipt, and decision timestamps in causal order. Missing IV is
rejected rather than forward-filled. Rows from another strategy are not
calibration evidence until their instrument, IV lineage, DTE policy, timing,
and evidence type have been audited.

The loader is fail-closed: identity and provenance are mandatory rather than
defaulted. A row must explicitly identify SOL, `SOL-USDC`/`SOL-PERP`, mainnet,
the call and put instruments, equal call/put/ATM strikes, a 2--14 DTE expiry,
the call and put IVs, independent call and put IV sources, an explicit SOL
reference price for the ATM strike-distance check, and the evidence type.
Incompatible rows
are reported as `INCOMPATIBLE_FOR_CALIBRATION`.

## Experiments

`STATIC_REGIME` reproduces the current NORMAL/DEFENSIVE static widths.
`IV_SCALED` evaluates a research-only width of

```text
clamp(K * ATM_IV * sqrt(width_horizon_seconds / YEAR_SECONDS), min, max)
```

Threshold, width, level-count, and defensive-quote candidates are evaluated
chronologically. Hysteresis is classified once over the full chronological
sequence and then sliced into the 60% development, 20% validation, and 20%
holdout partitions. Threshold candidate selection uses development and
validation only, requires per-state sample/outcome counts, the explicit
monotonic volatility gate (`HIGH RV > NORMAL RV` and `EXTREME RV >= HIGH RV`),
and stable nearby thresholds across all four threshold dimensions. A threshold
candidate is frozen only before the untouched holdout is read, and the final
status is one of `INSUFFICIENT_DATA`, `NO_ROBUST_CANDIDATE`,
`HOLDOUT_REJECTED`, or `CANDIDATE_FOR_SHADOW_VALIDATION`.
Neighbor distance is measured by configured-grid index, so irregular candidate
axes still mean “one configured step away” rather than one arbitrary numeric
distance.

Grid candidates include width policy, both width parameters, both level counts,
and the defensive quote fraction. Their touch metrics are proxy-only. The
current data contract has no fees, fills, inventory, or depth, so a proxy-stable
grid region is reported as `NOT_ECONOMICALLY_IDENTIFIABLE`; it is not a quote
fraction recommendation and no grid candidate is frozen for promotion.

State metrics are reported separately for NORMAL, HIGH, and EXTREME. Episode
statistics use elapsed time between observations and include episode count,
total duration, mean duration, and median duration. Realized volatility is
annualized as `sqrt(sum(log_return**2) * YEAR_SECONDS / elapsed_seconds)`.

## Evidence labels

Midpoint touch counts and rates are `SIMULATED_MID_TOUCH`. The proxy
uses only strictly post-decision prices and excludes the symmetric center level;
it is not maker-fill evidence. `REAL_EXECUTOR_FILL` is reserved for native
Hummingbot executor evidence with an order identifier. Without depth, fee, and
real order records, fees, turnover, PnL, drawdown, inventory accumulation, and
quote-fraction economics remain unavailable rather than being estimated as
facts. The evidence vocabulary is closed; arbitrary labels are rejected.

## Running it

```text
python scripts/calibrate.py --input data/calibration/observations.jsonl
```

The command writes CSV, JSON, and Markdown results under
`reports/calibration/`. With the repository’s current empty data directory it
reports `INSUFFICIENT_DATA`, identifies the missing history, and keeps all
strategy defaults unchanged.

## Continuous shadow capture

The controller exposes `calibration_observation_ready`,
`calibration_observation`, and `calibration_observation_errors` in
`get_custom_info()`. The controller never writes files. Condor's
`derive_options_grid_shadow_capture` routine persists only a ready observation
from the disarmed bot to the configured JSONL path. It fails closed when the
perpetual/options inputs or provenance are unavailable, preserves timestamps,
does not forward-fill IV, and rejects duplicate or out-of-order rows.

Use the reviewed gates for the entire collection campaign:

```text
mainnet_armed=false
execution_enabled=false
manual_kill_switch=false
evidence=SHADOW_PLAN
```

The saved rows support regime and midpoint-touch research only. They do not
prove fills, queue position, fees, sizing economics, or live profitability.
