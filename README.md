# Derive Options Adaptive Grid

SOL-USDC Derive mainnet perpetual adaptive grid for Hummingbot Condor.

The strategy reads public Derive SOL option IV to classify the market:

| State | Grid mode | Behavior |
| --- | --- | --- |
| `INITIALIZING` | `NORMAL` | Warm up a causal IV history; readiness blocks executor creation |
| `LOW` | `AGGRESSIVE` | Narrower, more granular provisional grid plan |
| `NORMAL` | `NORMAL` | Normal-width, normal-size grid plan |
| `HIGH` / `EXTREME` | `DEFENSIVE` | Wider and smaller grid plan |

The controller uses the native Hummingbot `GridExecutor`. Derive is ONEWAY, so
the controller has an explicit `oneway_side` and emits at most one active grid
executor. The pure planner still reports both-side geometry, but it never
assumes that opposite-side executors safely share one net position.

Each native executor receives Hummingbot's generated unique `id`; the stable
`sol_grid_buy`/`sol_grid_sell` values are used only as `level_id` reconciliation
keys. The full projected grid is capped by same-side soft and hard inventory
headroom, and creation is suppressed when the remaining headroom is below the
effective minimum order.

Live sizing uses the available USDC balance rather than a static collateral
floor: it reserves the greater of 20% or 20 USDC, deploys at most 50% of what
remains, keeps a 5 USDC minimum fee buffer, and still respects the configured
absolute quote and position caps. The controller publishes the capital
snapshot, fee assumptions, maker-price check, native minimum, entry/exit
semantics, account cleanliness, and live-readiness blockers to Condor.

The checked-in mainnet example is disarmed: `mainnet_armed: false`,
`execution_enabled: false`, and `manual_kill_switch: false`. Condor only reads
Hummingbot bot status and renders diagnostics. No live order, cancellation,
deployment, or arming is performed by the repository's verification workflow.
When the data gates are healthy, this disarmed configuration still computes
and displays the intended market state, grid mode, selected leg, and
`WOULD_CREATE` evidence; it returns no mutation actions.

The live-candidate example is intentionally non-executable. The repository now
contains a reproducible derived Hummingbot bot image under
`runtime/derive-options-grid-hummingbot/`. Its build is pinned to the inspected
Hummingbot image and proves `OPEN -> reduce_only=false`,
`CLOSE -> reduce_only=true`, `LIMIT -> gtc`, and `LIMIT_MAKER -> post_only`
with transport stubbed. Connector proof flags are accepted only as legacy
false values; a YAML value of `true` is rejected and runtime readiness never
trusts those fields.

The inspected Hummingbot source shows that `TradeFeeSchema` values are already
decimal fractions, while the upstream Derive defaults were `0.01/0.03` and
therefore did not match Derive instrument metadata `0.0001/0.0003`. The
derived image corrects those connector defaults to `0.0001/0.0003`. The
controller compares normalized decimal values without blindly dividing by 100
and exposes raw, normalized, and metadata observations to Condor.

The static connector contract does not prove a complete exchange lifecycle.
Without an already-configured Derive testnet account, lifecycle readiness
remains blocked and no mainnet canary is authorized.

See [docs/DESIGN.md](docs/DESIGN.md) and
[docs/RUNTIME_COMPATIBILITY.md](docs/RUNTIME_COMPATIBILITY.md), plus
[docs/CONDOR.md](docs/CONDOR.md) for installation and read-only monitoring.

## Layout

- `controllers/market_making/derive_options_adaptive_grid.py` — Hummingbot V2
  controller and native `GridExecutorConfig` adapter.
- `src/derive_options_adaptive_grid/` — causal SOL option-IV, state, mode,
  grid, model, evidence, and offline research logic with no exchange mutation.
- `condor/` — read-only continuous health, one-shot evidence, and persistent
  shadow-capture routines.
- `runtime/derive-options-grid-hummingbot/` — pinned, fail-closed derived bot
  image that repairs and verifies the native Derive connector contract.
- `configs/` — disarmed mainnet-shaped example.
- `data/calibration/` — the causal shadow-observation contract; raw captures are
  not checked in.
- `reports/calibration/` — reproducible calibration outputs, including explicit
  data limitations and holdout results.
- `scripts/` — marker-protected controller installation, non-mutating runtime
  contract probe, marker-protected Condor-routine installation, and offline
  calibration CLI.

## Verification

```text
python -m pytest
python -m compileall -q src controllers condor
python scripts/controller_contract_probe.py   # inside the Hummingbot runtime
scripts/install_controller.sh --dry-run
scripts/install_condor_routines.sh --dry-run
python scripts/calibrate.py --input data/calibration/observations.jsonl
```

The optional public-data smoke test, when run manually, must use only the two
allowlisted public Derive methods and must never include credentials.
