# Derived Hummingbot runtime for the Derive adaptive grid

This directory owns a reproducible bot-image derivation for the Derive
SOL-USDC adaptive grid. It starts from the exact inspected
`hummingbot/hummingbot` image digest and refuses to patch an unknown source
tree.

The image-build patch makes the native connector contract explicit:

- `PositionAction.OPEN` sends `reduce_only=false`.
- `PositionAction.CLOSE` sends `reduce_only=true`.
- `OrderType.LIMIT` sends `time_in_force=gtc`.
- `OrderType.LIMIT_MAKER` sends `time_in_force=post_only`.
- Derive's mainnet fee defaults are stored as decimal fractions matching the
  instrument metadata: maker `0.0001`, taker `0.0003`.

The patch is not a strategy-local HTTP client or a runtime monkey patch. The
build fails if the base image source hashes change. The verification script
uses a transport stub and never signs, sends, cancels, or modifies an account.

Build a separately tagged local image from this directory. Do not overwrite
`hummingbot/hummingbot:latest`. A fresh shadow-only bot may then select the
derived tag through the existing `V2ControllerDeployment.image` field.

The static image check does not claim an exchange lifecycle proof. A bounded,
authenticated Derive testnet lifecycle remains a separate gate and must end
with zero position, orders, and executors.
