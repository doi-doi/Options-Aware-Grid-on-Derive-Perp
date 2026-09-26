# SOL-USDC options-IV adaptive grid

## Goal

Build a Hummingbot V2 controller for Derive mainnet `SOL-USDC` perpetuals
(`SOL-PERP`) that uses a fresh public Derive SOL option ATM implied-volatility
observation to select a market state and a native Hummingbot `GridExecutor`
mode. The Condor layer will observe and report the decision state; it is not a
second order engine.

## Context

The installed Hummingbot Derive connector provides the perpetual market and
account runtime. The option IV input is obtained separately through Derive's
public `public/get_instruments` and `public/get_tickers` methods because the
perpetual connector does not expose the option chain to a controller. The pure
layer keeps these facts independent so a healthy perpetual BBO cannot mask an
unavailable or stale option chain.

## Constraints

- The supported execution tuple is exactly `derive_perpetual` / `SOL-USDC` /
  Derive exchange instrument `SOL-PERP`.
- The public option adapter is SOL-only, read-only, and allowlisted to the two
  public endpoints above. It records source, receipt, decision timestamps,
  expiry, strike, IV source, age, and validation errors.
- Future-dated and out-of-order observations are rejected. Missing options are
  not forward-filled; they fail the readiness gates while the intended market
  state and grid mode remain visible.
- `INITIALIZING` warms up on a bounded causal IV history. `LOW` selects the
  narrower aggressive geometry, `NORMAL` selects the normal geometry, and
  `HIGH`/`EXTREME` select a wider/smaller defensive geometry. Stale data, risk
  gates, or manual disarm produce no executor actions without erasing the
  intended mode.
- Public data, a shadow plan, and a proxy touch are not fills. Only an explicit
  native executor fill will be recorded as `REAL_EXECUTOR_FILL`.
- The installed Derive connector supports only `PositionMode.ONEWAY` and its
  current order adapter sends `reduce_only: false` for both open and close.
  The controller therefore
  has an explicit `oneway_side`, permits at most one strategy-owned active grid
  executor, and blocks incompatible existing inventory. It does not claim that
  simultaneous BUY and SELL executors are safe.
- The controller requires the Derive connector's own `ready` flag, not only a
  cached BBO, rule, or balance. It sizes the complete projected grid against
  remaining same-side soft and hard position headroom and blocks with an
  explicit reason when that headroom is below the effective minimum order.
- Hummingbot generates each executor `id`; stable side values are `level_id`
  reconciliation keys. The manual kill switch is an emergency cash-out/stop
  control for the generic runner and defaults to false for a continuously
  running shadow. Mainnet arming remains a separate review decision.
- Shadow planning is independent from execution authorization: healthy market,
  option, connector, inventory, collateral, executor, and grid-size inputs can
  produce an AGGRESSIVE/NORMAL/DEFENSIVE plan and `WOULD_CREATE` evidence while the three
  authorization flags still return zero mutation actions. Those flags do not
  erase the diagnostic plan.
- A controller stop uses `keep_position=True`: it cancels entry orders and
  leaves any filled position held because the installed Derive connector's
  close path currently sends `reduce_only: false`. Any flattening action must
  be separately reviewed; the strategy does not claim reduce-only stop safety.
- Autonomous executor timeouts are disabled by default (`time_limit_seconds:
  null`) because the installed timeout path submits a market close through the
  same non-reduce-only connector behavior.
- The controller maps to native `GridExecutorConfig` only. Condor is a status
  observer and never becomes a second order engine.
- Capital allocation is dynamic and fail-closed. The controller reads
  `get_available_balance("USDC")`, uses only a valid `available_balances["USDC"]`
  fallback, and records reserve, deployable, target, fee-buffer, selected-leg,
  soft-cap, and hard-cap values as an immutable `CapitalSnapshot`.
- Execution reconciliation keeps the intended grid visible while deriving a
  separate executable grid. If an active managed executor becomes unsafe,
  live execution emits one `StopExecutorAction(..., keep_position=True)` and
  never creates a replacement in that cycle. A capital decrease is therefore
  a safety event; a small increase does not churn the grid.
- The installed connector contract is a hard gate, not a configuration claim.
  The checked-in probe invokes the exact runtime request builder with a
  transport stub and records the source path and hash. Until close
  `reduce_only`, maker `post_only`, and the fee/lifecycle checks are proven,
  Condor reports `BLOCKED` and the candidate remains disarmed. Legacy proof
  fields are false-only migration inputs; true assertions are rejected and
  are never used to enable readiness.
- Fee readiness requires agreement between the installed Hummingbot fee
  schema and Derive instrument metadata. A material disagreement preserves
  both raw values, reports `fee_model_status=UNKNOWN`, omits the economic
  take-profit floor, and adds `BLOCKED_BY_FEE_MODEL` plus
  `fee_source_mismatch`.
- Mainnet execution remains gated by `execution_enabled`, `mainnet_armed`, and
  a cleared manual kill switch. The checked-in example keeps all gates safe.

## Done when

The target repository contains the native controller, disarmed mainnet example,
read-only Condor routines, marker-protected installation path, runtime contract
probe, and focused tests. Verification passes without changing the parent
checkout or placing an order.
