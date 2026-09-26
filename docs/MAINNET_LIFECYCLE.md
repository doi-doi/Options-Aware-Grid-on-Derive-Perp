# Derive mainnet lifecycle gate

The adaptive grid remains a Hummingbot-native `GridExecutor` controller for
`derive_perpetual / SOL-USDC`.  The controller can calculate options-IV state,
grid mode, inventory, capital, and pricing in shadow mode, but it does not
consider the connector lifecycle verified from static source inspection alone.

The lifecycle gate requires all of the following to agree:

- the fixed mainnet/pair/instrument/ONEWAY/leverage identity;
- the derived-image digest, runtime contract ID, connector source hash, fee
  source hash, and runtime image reference; the concrete canary container
  identity is retained as provenance but is not required to match a later
  controller restart;
- the independent wire contract: OPEN `reduce_only=false`, CLOSE
  `reduce_only=true`, LIMIT `gtc`, LIMIT_MAKER `post_only`, maker `0.0001`,
  taker `0.0003`;
- a hashed account identity with a clean starting account;
- separately reviewed Stage A and Stage B results; and
- an independently read final cleanup with zero position, active orders, and
  managed or unmanaged executors.

The promoted evidence path is fixed inside the bot at:

```text
/home/hummingbot/controllers/market_making/derive_options_adaptive_grid_lifecycle/derive_sol_mainnet.json
```

The source-approved SHA is intentionally empty in the current iteration, so
the controller must report `lifecycle_verified=false` and
`BLOCKED_BY_UNVERIFIED_CONNECTOR_LIFECYCLE`.  Do not create a placeholder
artifact or change that source constant during read-only preflight.

The canary uses the connector's public owner and subaccount identity only in
memory, hashes it, and records only the resulting fingerprint. Account, BBO,
trading-rule, static-contract, and runtime sections must carry the same
container identity and runtime contract ID. A snapshot assembled from
different bot instances is rejected before any stage runs.

## Read-only checks

The canary is dry-run by default and reports proposed Stage A and Stage B
prices, amounts, notional, and blockers without signing, submitting, or
cancelling anything:

```bash
python scripts/derive_options_grid_mainnet_lifecycle_canary.py --mode DRY_RUN
```

Inside a Hummingbot script, `build_runtime_snapshot(connector)` is the
runtime-bound capture hook. It reads the existing connector's in-memory
account, order-book, and trading-rule state without issuing a private REST
request.

Install the script into the API bot workspace without starting a bot:

```bash
scripts/install_lifecycle_canary.sh --dry-run
scripts/install_lifecycle_canary.sh --apply
```

The Condor health, evidence, and shadow-capture routines are read-only.  They
surface lifecycle evidence presence, approval, runtime identity, stage status,
cleanup status, and blockers beside the live IV regime and grid metrics.

Promotion is a later, explicit review step.  It canonicalizes and hashes a
complete evidence document, and atomically installs it only when the reviewed
SHA is supplied.  It does not change the source-approved SHA or authorize a
bot by itself:

```bash
python scripts/promote_lifecycle_evidence.py evidence.json \
  --expected-sha256 REVIEWED_SHA256
```

Stage A and Stage B are intentionally separate.  Any future mutation must use
normal Hummingbot connector/order/executor APIs and must supply explicit stage
authorization plus a one-run token outside the repository.  Missing or wrong
authorization always results in `mutation_attempted=false`.

Stage A re-reads the account, runtime binding, trading rules, and BBO immediately
before its single post-only request.  Its quote uses the larger of the
configured native-tick and basis-point maker guards (five ticks and 15 bps by
default).  The guard is specific to this one-order canary; adaptive-grid
pricing is unchanged.  A post-only rejection is reconciled as a failed
pre-acceptance attempt without retrying or submitting a replacement order.

The pure `run_stage_a` and `run_stage_b` state machines are covered by local
stubs. The installed CLI permits only the separately authorized Stage A path;
Stage B remains a separate reviewed action, and ordinary bot startup defaults
to the read-only path.
