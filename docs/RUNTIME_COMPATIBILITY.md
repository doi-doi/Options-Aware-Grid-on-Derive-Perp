# Runtime compatibility record

The following was inspected in the local `hummingbot-api` container on
2026-09-20.

- Python: 3.12.14.
- `GridExecutorConfig` module:
  `hummingbot.strategy_v2.executors.grid_executor.data_types`.
- `GridExecutor` module:
  `hummingbot.strategy_v2.executors.grid_executor.grid_executor`.
- Required grid fields include `connector_name`, `trading_pair`,
  `start_price`, `end_price`, `limit_price`, `side`, `total_amount_quote`,
  `min_spread_between_orders`, `min_order_amount_quote`, `max_open_orders`,
  `max_orders_per_batch`, `order_frequency`, `activation_bounds`,
  `triple_barrier_config`, `leverage`, `level_id`, and `keep_position`.
- `GridExecutor.get_custom_info()` exposes level counts, order lists,
  realized buy/sell quote, realized fees/PnL, position metrics, and grid
  visualization prices.
- The installed executor uses a zero `limit_price` as the disabled value. A
  non-zero limit price is a position-stop condition, so the pure plan uses
  `Decimal("0")` and leaves state-change stop/rebuild behavior to the
  controller.
- The installed controller action surface is
  `CreateExecutorAction(controller_id=..., executor_config=...)` and
  `StopExecutorAction(controller_id=..., executor_id=..., keep_position=...)`.

The controller iteration must still validate imports and config construction in
the exact Condor/Hummingbot process before any arming decision. This document is
an observation record, not live-trading authorization.

## Derive one-way lifecycle gate (2026-09-20)

The installed Derive perpetual connector reports only `PositionMode.ONEWAY` from
`supported_position_modes()`. Its current `_place_order()` implementation sends
`reduce_only: false` for both `PositionAction.OPEN` and `PositionAction.CLOSE`;
the `position_action` value is not translated into an exchange reduce-only flag.
The native `GridExecutor` does use `PositionAction.OPEN` for its entry orders and
`PositionAction.CLOSE` for its close orders, and `early_stop(keep_position=False)`
cancels open orders before shutting the executor down. That confirms the native
executor lifecycle, but it does not prove that two opposite-side grids can safely
share one Derive one-way position: an opposite-side entry can increase or flip
the net position instead of being a guaranteed close.

This strategy therefore refuses to create simultaneous BUY and SELL grid
executors. It has an explicit `oneway_side` policy, caps the strategy at one
active grid executor, and blocks when existing net inventory is incompatible
with the configured side or hard limit. The strategy still computes both-side
pure grid diagnostics, but the Hummingbot controller emits at most one native
GridExecutor action. No alternative executor or direct order client is used.

The Derive connector's `LIMIT_MAKER` mapping was also inspected: it maps to a
GTC limit request with the connector's limit-maker order type selection. This
implementation preserves that native mapping and does not claim independent
exchange-side post-only proof. A safe live canary remains a separate operator
decision.

The installed Hummingbot fee schema and the public Derive instrument metadata
are currently different observations: the exact runtime probe reported maker
`0.01` and taker `0.03`, while instrument metadata reported `0.0001` and
`0.0003`. Neither source is silently preferred. The controller preserves both
raw observations, reports `fee_model_status=UNKNOWN`, leaves
`minimum_economic_take_profit_pct` unset, and blocks with
`BLOCKED_BY_FEE_MODEL` plus `fee_source_mismatch`. This mismatch must be
resolved by an operator before any live canary.

Connector close, post-only, and lifecycle proof are runtime evidence rather
than configuration. Legacy proof fields may remain false in older YAML, but a
true value is rejected and the controller's readiness gates do not read those
fields as permissions.

## Controller safety gates (2026-09-20)

The controller requires the connector's native `ready` flag in addition to the
cached order book, trading rule, balance, and account-position reads. It
exposes this as `risk_gates.connector_ready` and blocks executor readiness when
it is false.

The complete projected grid is bounded by remaining same-side `max_position_quote`
and `hard_position_quote` headroom. When the remaining headroom is below the
effective native minimum order, the grid is not executable and the diagnostic
reason is `projected_inventory_headroom_below_minimum_grid_order`.

Each `GridExecutorConfig` leaves `id` unset so Hummingbot generates a unique
executor identifier. The controller's `sol_grid_buy`/`sol_grid_sell` values are
only stable `level_id` keys. `manual_kill_switch` carries
`json_schema_extra={"is_updatable": true}` for Hummingbot config reloads.

The checked-in executor policy sets `keep_position=True` and
`triple_barrier_config.time_limit=None`. The first preserves a held-position
policy for the executor's autonomous limit-price barrier, and the second
disables the autonomous timeout because the installed executor's time-limit
path calls `place_close_order_and_cancel_open_orders()` with a MARKET close.
The configured take-profit/stop-loss close paths remain native Hummingbot
behavior and are not claimed to be exchange-side reduce-only; this is a live
execution residual risk, not a reason to modify the connector here.

The inspected `early_stop(keep_position=False)` path cancels open orders and,
when a position remains, submits a market close with `PositionAction.CLOSE`.
The Derive connector still sends `reduce_only: false` for that path. This
controller therefore emits `StopExecutorAction(..., keep_position=True)` so a
state-change stop cancels entries without silently requesting a potentially
non-reduce-only flatten. A held position remains an explicit operator risk and
must be handled separately.

The live-readiness contract also requires a separate connector lifecycle proof:
the controller configuration keeps `connector_lifecycle_verified=false` until
open/close, post-only, stop, and restart behavior has been verified against the
exact deployed runtime. The diagnostics expose this as
`BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE`; no configuration in this
repository enables that flag.

## Derived connector runtime (2026-09-21)

The inspected bot image is pinned by digest
`sha256:632d2b07aa156b761310f2f7258a78c9660a1c28b6df4b33874e09a0c7d06c85`.
The Derive connector source is supplied by that image rather than vendored in
this strategy repository. The repository therefore owns a deterministic
derived-image patch with exact base-source SHA guards; it refuses unknown
connector source instead of mutating a running container or the floating
`latest` image.

The image-build verification proves, with transport stubbed:

- `PositionAction.OPEN` sends `reduce_only=false`.
- `PositionAction.CLOSE` sends `reduce_only=true`.
- `OrderType.LIMIT` sends `time_in_force=gtc`.
- `OrderType.LIMIT_MAKER` sends `time_in_force=post_only`.

The exact fee implementation also proves that `TradeFeeSchema` values are
already decimal fractions. The upstream Derive defaults `0.01` and `0.03`
were therefore corrected in the derived image to `0.0001` and `0.0003`, which
match the instrument metadata observed by the initialized controller. No
strategy-local fee conversion or order client is used.

The static contract is not an exchange lifecycle proof. Testnet availability,
post-only acceptance, cancellation, fill reconciliation, reduce-only close,
no-flip enforcement, and clean final exposure remain separate evidence gates.
