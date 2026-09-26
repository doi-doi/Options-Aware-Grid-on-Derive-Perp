# Shadow calibration data contract

The calibration command accepts `observations.jsonl` (or an explicit JSON/CSV
path) containing one causal record per accepted controller decision. The
canonical fields are:

- `decision_timestamp`, `source_timestamp`, `received_timestamp`
- `perp_mid`, `best_bid`, `best_ask`
- `underlying=SOL`, `trading_pair=SOL-USDC`, `exchange_instrument=SOL-PERP`,
  `environment=mainnet`
- `atm_call_iv`, `atm_put_iv`, and optionally `atm_iv`
- `expiry_timestamp`, `days_to_expiry`, `atm_strike`, `call_strike`,
  `put_strike`, `call_instrument`, and `put_instrument`
- independent `call_iv_source` and `put_iv_source` values; `iv_source` is
  optional display metadata, not a substitute for either independent source
- `option_reference_price`, the SOL price used to enforce the maximum 5% ATM
  strike distance
- `source` and a closed-vocabulary `evidence` value (for example
  `SHADOW_PLAN` or `PUBLIC_MARKET_DATA`)

Rows must be strictly chronological. Source time must be no later than receipt
time, and receipt time must be no later than decision time. Both same-strike
SOL call and put IV values are required. `call_strike`, `put_strike`, and
`atm_strike` must all be present and equal, and the expiry/DTE must be between
2 and 14 days. The nearest ATM strike must be within 5% of
`option_reference_price`. `REAL_EXECUTOR_FILL` additionally requires a native
order identifier. Missing identity or provenance is rejected as
`INCOMPATIBLE_FOR_CALIBRATION`; it is never silently defaulted. Missing IV is
rejected; it is never forward-filled. The repository does not store live
credentials or private API responses.

The shadow append helper validates each row before writing and refuses
duplicates or out-of-order observations.

Run the research-only pipeline with:

```text
python scripts/calibrate.py --input data/calibration/observations.jsonl
```

The resulting reports distinguish `SIMULATED_MID_TOUCH` from
`REAL_EXECUTOR_FILL`. Midpoint touch counts are not fills, PnL, or live trading
evidence. Large/raw captures should remain local and gitignored unless a
reviewed fixture is intentionally added.
