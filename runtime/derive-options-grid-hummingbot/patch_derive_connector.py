#!/usr/bin/env python3
"""Apply the reviewed Derive connector contract at image-build time.

The script refuses unknown connector source.  It is deterministic and
idempotence is intentionally *not* implicit: a second patch attempt against
already-modified source fails instead of silently drifting the image.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from decimal import Decimal
from pathlib import Path

BASE_ORDER_SOURCE_SHA256 = "73f8ef2a79b51696e87c2507ebec0d3073750586e9559fcc7190522381dd7340"
BASE_UTILS_SOURCE_SHA256 = "3bd60870057599ba643797a73a00d642dd4fe48c93d25736fd471634f4182632"
BASE_IMAGE_DIGEST = "sha256:632d2b07aa156b761310f2f7258a78c9660a1c28b6df4b33874e09a0c7d06c85"

ORDER_RELATIVE_PATH = Path(
    "connector/derivative/derive_perpetual/derive_perpetual_derivative.py"
)
UTILS_RELATIVE_PATH = Path("connector/derivative/derive_perpetual/derive_perpetual_utils.py")

OLD_ORDER_BLOCK = (
    "        if order_type is OrderType.LIMIT and position_action == "
    "PositionAction.CLOSE:\n"
    '            param_order_type = "gtc"\n'
    "        elif order_type is OrderType.LIMIT_MAKER:\n"
    '            param_order_type = "gtc"\n'
)
NEW_ORDER_BLOCK = '''        if order_type is OrderType.LIMIT:
            param_order_type = "gtc"
        elif order_type is OrderType.LIMIT_MAKER:
            param_order_type = "post_only"
'''
OLD_REDUCE_ONLY = '            "reduce_only": False,\n'
NEW_REDUCE_ONLY = '            "reduce_only": position_action == PositionAction.CLOSE,\n'
OLD_MAKER_FEE = '    maker_percent_fee_decimal=Decimal("0.01"),\n'
NEW_MAKER_FEE = '    maker_percent_fee_decimal=Decimal("0.0001"),\n'
OLD_TAKER_FEE = '    taker_percent_fee_decimal=Decimal("0.03"),\n'
NEW_TAKER_FEE = '    taker_percent_fee_decimal=Decimal("0.0003"),\n'


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_contract_id(
    base_image_digest: str, patched_order_source_sha256: str, patched_utils_source_sha256: str
) -> str:
    identity = {
        "base_image_digest": base_image_digest,
        "connector_source_sha256": patched_order_source_sha256,
        "fee_source_sha256": patched_utils_source_sha256,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one {label}, found {count}")
    return source.replace(old, new, 1)


def patch(root: Path, manifest_path: Path) -> dict[str, str]:
    order_path = root / ORDER_RELATIVE_PATH
    utils_path = root / UTILS_RELATIVE_PATH
    if not order_path.is_file() or not utils_path.is_file():
        raise RuntimeError("Derive connector source paths are missing")
    if sha256(order_path) != BASE_ORDER_SOURCE_SHA256:
        raise RuntimeError("Derive order source hash is not the reviewed base image")
    if sha256(utils_path) != BASE_UTILS_SOURCE_SHA256:
        raise RuntimeError("Derive utility source hash is not the reviewed base image")

    order_source = order_path.read_text()
    order_source = replace_once(
        order_source, OLD_ORDER_BLOCK, NEW_ORDER_BLOCK, "order-type mapping"
    )
    order_source = replace_once(
        order_source, OLD_REDUCE_ONLY, NEW_REDUCE_ONLY, "reduce-only mapping"
    )
    utils_source = utils_path.read_text()
    utils_source = replace_once(
        utils_source, OLD_MAKER_FEE, NEW_MAKER_FEE, "maker fee default"
    )
    utils_source = replace_once(
        utils_source, OLD_TAKER_FEE, NEW_TAKER_FEE, "taker fee default"
    )

    ast.parse(order_source, filename=str(order_path))
    ast.parse(utils_source, filename=str(utils_path))
    order_path.write_text(order_source)
    utils_path.write_text(utils_source)
    manifest = {
        "base_image_digest": BASE_IMAGE_DIGEST,
        "base_order_source_sha256": BASE_ORDER_SOURCE_SHA256,
        "base_utils_source_sha256": BASE_UTILS_SOURCE_SHA256,
        "patched_order_source_sha256": sha256(order_path),
        "patched_utils_source_sha256": sha256(utils_path),
        "runtime_contract_id": runtime_contract_id(
            BASE_IMAGE_DIGEST, sha256(order_path), sha256(utils_path)
        ),
        "order_source": str(order_path),
        "utils_source": str(utils_path),
        "fee_unit": "decimal_fraction",
        "mainnet_maker_fee_decimal": str(Decimal("0.0001")),
        "mainnet_taker_fee_decimal": str(Decimal("0.0003")),
        "contract": {
            "open_reduce_only": False,
            "close_reduce_only": True,
            "limit_time_in_force": "gtc",
            "limit_maker_time_in_force": "post_only",
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/home/hummingbot/hummingbot"),
        help="installed Hummingbot package root",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/opt/derive-options-grid-runtime/runtime_contract_manifest.json"),
    )
    args = parser.parse_args()
    patch(args.root, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
