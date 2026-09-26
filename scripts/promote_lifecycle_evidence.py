#!/usr/bin/env python3
"""Validate and optionally install one reviewed lifecycle evidence artifact.

Promotion is intentionally a separate, explicit operation.  This command is
not used by the mainnet preflight and cannot make the controller ready unless
the source constant is reviewed and updated separately.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root / "src") not in sys.path:
    sys.path.insert(0, str(repo_root / "src"))

from derive_options_adaptive_grid.lifecycle_evidence import (  # noqa: E402
    HOST_PROMOTED_LIFECYCLE_EVIDENCE_PATH,
    LifecycleEvidence,
    validate_lifecycle_evidence,
)


def promote(input_path: Path, output_path: Path, expected_sha256: str, *, apply: bool) -> str:
    evidence = LifecycleEvidence.from_json(input_path.read_text(encoding="utf-8"))
    actual_sha256 = evidence.sha256()
    if actual_sha256 != expected_sha256:
        raise ValueError("reviewed SHA256 does not match canonical evidence")
    validation = validate_lifecycle_evidence(
        evidence,
        approved_sha256=expected_sha256,
        # Promotion is only a canonicalization/install step; an independent
        # runtime/account match is still required when the controller loads it.
        expected_runtime=None,
        expected_account={
            "account_fingerprint": evidence.as_dict()["account"]["account_fingerprint"]
        },
    )
    if not validation.valid:
        raise ValueError("evidence is not complete: " + ", ".join(validation.blockers))
    if not apply:
        return actual_sha256
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(evidence.canonical_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output_path)
    finally:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass
    return actual_sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument(
        "--output", type=Path, default=HOST_PROMOTED_LIFECYCLE_EVIDENCE_PATH
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    digest = promote(args.input, args.output, args.expected_sha256, apply=args.apply)
    print(
        {
            "status": "PROMOTED" if args.apply else "VALID_FOR_PROMOTION",
            "sha256": digest,
            "output": str(args.output),
            "mutation": "artifact_install_only" if args.apply else "none",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
