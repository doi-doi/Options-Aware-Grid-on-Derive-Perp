# Condor installation and monitoring

This repository is designed to be installed into an existing Hummingbot API /
Condor checkout. Hummingbot remains the only execution runtime. Condor routines
read `bot_orchestration.get_bot_status()` and display the controller's
`get_custom_info()` contract; they do not call Derive directly and have no ARM,
create, stop, cancel, or configuration-edit action.

## Install the controller source

From this repository:

```text
scripts/install_controller.sh --dry-run
HUMMINGBOT_API_DIR=/path/to/hummingbot-api scripts/install_controller.sh --apply
scripts/install_condor_routines.sh --dry-run
CONDOR_DIR=/path/to/condor scripts/install_condor_routines.sh --apply
```

The installer copies the controller to
`bots/controllers/market_making/` and the pure package to a dedicated support
directory. It refuses to overwrite an unmarked existing file. It does not copy
or change any active bot configuration and does not start a bot.

Run the contract probe with the Python environment used by Hummingbot:

```text
HUMMINGBOT_API_DIR=/path/to/hummingbot-api \
  /path/to/hummingbot-python scripts/controller_contract_probe.py
```

The probe only imports the controller and validates its exact connector,
market, instrument, environment, one-way mode, leverage, and disarmed defaults.

## Derived bot image

Build the separately tagged runtime from
`runtime/derive-options-grid-hummingbot/`. The Dockerfile is pinned to the
inspected Hummingbot image digest and the build runs the non-network connector
payload check. Select the resulting image only for a fresh disarmed shadow
through `V2ControllerDeployment.image`; never overwrite
`hummingbot/hummingbot:latest`.

The runtime contract probe is intentionally transport-stubbed. It proves the
native OPEN/CLOSE reduce-only mapping and LIMIT/LIMIT_MAKER time-in-force
mapping, but it cannot prove exchange lifecycle behavior. Condor displays the
runtime source hash, raw and normalized fee rates, order TIF values, static
contract status, lifecycle status, and the exact remaining blockers.

The Condor-routine installer is separate and marker/hash protected. It installs
only `derive_options_grid_health`, `derive_options_grid_evidence`, and
`derive_options_grid_shadow_capture` into the existing Condor `routines/`
directory. It never starts a routine. If an existing file has changed outside
this repository, the installer refuses to overwrite it.

## Add the controller to Condor/Hummingbot

Use the normal Condor/Hummingbot controller management workflow to load
`configs/derive_options_adaptive_grid_sol_shadow.yml` under the reviewed
configuration name `derive_options_adaptive_grid_sol_shadow`, then deploy the
bot as `derive-options-adaptive-grid-sol-shadow` only after reading the saved
configuration back. The configuration is intentionally:

```text
mainnet_armed=false
execution_enabled=false
manual_kill_switch=false
```

Condor may append a timestamp to the requested bot name when it creates the
runtime instance. Use the returned `unique_instance_name` (for example,
`derive-options-adaptive-grid-sol-shadow-YYYYMMDD-HHMMSS`) as `bot_name` for
every health, evidence, and capture routine configuration.

Keep those values while validating the dashboard and option-data feed. Start
the bot only in this disarmed shadow form. If the existing account/profile does
not provide usable `derive_perpetual` mainnet connectivity, stop with
`BLOCKED_BY_ACCOUNT_CONFIGURATION`; do not create or change credentials.

Immediately after deployment, verify from bot status and controller diagnostics:

```text
mainnet_armed=false
execution_enabled=false
manual_kill_switch=false
execution_state=SHADOW_RUNNING
executors=[]
reconciliation.creates=0
reconciliation.stops=0 (unless an existing startup cleanup is understood)
last_actions contains no CreateExecutorAction
real_executor_fill_count=0
```

`WOULD_CREATE` is a hypothetical plan signal, not an order. If a real executor,
order identifier, or `LIVE_ENABLED` appears, stop the deployment safely and do
not send further trading actions. Any later live authorization is an independent
operator decision and requires a separate Derive order-type, collateral,
inventory, and reduce-only review.

## Read-only routines

- `derive_options_grid_health` is a continuous `LiveReport`. It shows
  `PERP_READY`, `CONNECTOR_READY`, and `OPTIONS_READY` separately, then displays IV inputs,
  causal state/mode, inventory, available capital, entry/exit safety, fee economics,
  account cleanliness, live-readiness blockers, desired grid, risk gates,
  reconciliation, executor metrics, PnL, and evidence classifications. It also
  shows the installed connector proof status and both raw fee observations;
  unresolved fee-source differences are displayed as `UNKNOWN`, never as a
  verified economic take-profit floor.
- `derive_options_grid_evidence` is a one-shot status snapshot suitable for an
  audit or export. `REAL_EXECUTOR_FILL` is preserved separately from
  `PUBLIC_MARKET_DATA`, `SHADOW_PLAN`, `WOULD_CREATE`, `WOULD_STOP`, and
  `PROXY_TOUCH` evidence.
- `derive_options_grid_shadow_capture` is a continuous, read-only JSONL writer.
  It reads only the bot-status payload, refuses unsafe execution gates, rejects
  invalid/duplicate/out-of-order observations, flushes each successful append,
  and reports capture counters. `records_written` is not a fill count.

## Start the shadow capture

Start the health and capture routines for the shadow bot using the current
Condor routine workflow. Pass the capture path explicitly; the recommended
persistent runtime path is:

```text
data/derive_options_adaptive_grid/observations.jsonl
```

This is relative to the Condor project directory. Do not use a temporary path
for a multi-week campaign, and run only one collector instance for one output
file. The capture routine requires these exact gates in the running bot:

```text
mainnet_armed=false
execution_enabled=false
manual_kill_switch=false
```

The live report exposes the bot name, output path, records written, duplicate
and invalid/unready skips, unsafe-gate skips, last decision/source timestamps,
market state, IV ratio, capital, entry/exit safety, fee economics, account
cleanliness, readiness status, and the last error. It also labels every row as
`evidence=SHADOW_PLAN`.

To use the persistent file for research, reference it directly when the Condor
checkout is visible from the repository, or make a read-only local copy outside
Git. Then run:

```text
python scripts/calibrate.py --input /path/to/observations.jsonl
```

An early small capture should still report `INSUFFICIENT_DATA` and
`KEEP_CURRENT_DEFAULTS`; that is the expected result and does not justify
changing thresholds, widths, level counts, or quote sizing.

The minimum useful campaign is 2--4 weeks and the preferred horizon is 4--8
weeks. Distinct NORMAL/HIGH/EXTREME episodes matter more than the raw number of
five-second rows.

## Stop safely

Stop the capture routine by stopping its routine instance through Condor. Stop
the disarmed bot only through the normal Condor/Hummingbot bot lifecycle after
confirming that no strategy executor is active. Do not call a flatten, cancel,
or live-execution action as part of shadow collection. If an executor or order
appears unexpectedly, preserve the evidence and escalate the safety failure.

The health routine displays the three execution gates independently and labels
the runtime as `SHADOW_RUNNING`, `LIVE_DEARMED`, `LIVE_ENABLED`,
`CASHED_OUT`, or `UNKNOWN`:

- `SHADOW_RUNNING`: the controller is running with `mainnet_armed=false`,
  `execution_enabled=false`, and `manual_kill_switch=false`.
- `LIVE_DEARMED`: execution is enabled but mainnet arming is still false.
- `LIVE_ENABLED`: all three gates are clear. This label is diagnostic only;
  this repository's example remains disarmed and no live deployment is part of
  verification.
- `CASHED_OUT`: the generic manual kill switch is active and the controller
  may be stopped by the runner.

If options data is missing or stale while the perpetual feed is healthy, the
dashboard deliberately shows `PERP_READY=true` and `OPTIONS_READY=false`, and
the executor readiness gate remains blocked. A healthy process or a visible grid plan is not
evidence of a live fill.

When all planning inputs are healthy, the disarmed example still shows the
AGGRESSIVE/NORMAL/DEFENSIVE plan, selected one-way leg, projected inventory headroom, and
`WOULD_CREATE` reconciliation evidence. The execution flags keep actual action
lists empty. The controller's autonomous timeout is disabled in the example;
state-change stops use `keep_position=True` because Derive's current close
adapter does not provide a proven reduce-only flag.
