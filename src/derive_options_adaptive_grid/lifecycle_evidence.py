# DERIVE_OPTIONS_ADAPTIVE_GRID_MANAGED
"""Trusted, immutable evidence for the Derive connector lifecycle gate.

The static connector probe proves how the installed Hummingbot connector
*would* encode an order.  This module records the separate exchange-side
evidence required before that proof can authorize anything.  The promoted
artifact is deliberately fixed to one path and the approved source hash is
deliberately empty in this iteration.  A JSON file copied into a container
therefore cannot unlock the controller by itself.

No credentials, order identifiers, account addresses, or secrets belong in
the artifact.  Account identity is represented by a reviewed, one-way
fingerprint supplied by the lifecycle harness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

LIFECYCLE_EVIDENCE_SCHEMA_VERSION = 1
PROMOTED_LIFECYCLE_EVIDENCE_PATH = Path(
    "/home/hummingbot/controllers/market_making/"
    "derive_options_adaptive_grid_lifecycle/derive_sol_mainnet.json"
)
HOST_PROMOTED_LIFECYCLE_EVIDENCE_PATH = Path(
    "hummingbot-api/bots/controllers/market_making/"
    "derive_options_adaptive_grid_lifecycle/derive_sol_mainnet.json"
)

# This remains unset until the independent Stage A and Stage B review is
# complete.  It is intentionally a source constant, not a config value or an
# environment variable.
APPROVED_LIFECYCLE_EVIDENCE_SHA256: str | None = None

EXPECTED_IDENTITY = {
    "environment": "mainnet",
    "connector_name": "derive_perpetual",
    "trading_pair": "SOL-USDC",
    "exchange_instrument": "SOL-PERP",
    "position_mode": "ONEWAY",
    "leverage": 1,
}

EXPECTED_STATIC_CONTRACT = {
    "open_reduce_only": False,
    "close_reduce_only": True,
    "limit_tif": "gtc",
    "limit_maker_tif": "post_only",
    "maker_fee_decimal": "0.0001",
    "taker_fee_decimal": "0.0003",
}

_SENSITIVE_KEY_PARTS = ("secret", "private_key", "api_key", "passphrase", "mnemonic")
ACCOUNT_FINGERPRINT_SCHEME = "sha256:derive-perpetual-account-v1"

# A lifecycle artifact is collected by one concrete canary instance, but it
# must remain usable by a later controller instance built from the same
# reviewed runtime. The canary instance ID is retained as provenance; only
# these reusable build/contract fields participate in promoted-evidence
# matching.
REUSABLE_RUNTIME_IDENTITY_KEYS = (
    "base_image_digest",
    "runtime_contract_id",
    "connector_source_sha256",
    "fee_source_sha256",
    "runtime_image_ref",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_value(value: Any) -> Any:
    """Convert Decimal and immutable containers to strict JSON values."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical_value(item) for item in value), key=repr)
    return value


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return the stable JSON representation used for the artifact hash."""

    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_canonical(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compute_runtime_contract_id(
    base_image_digest: str | None,
    connector_source_sha256: str | None,
    fee_source_sha256: str | None,
) -> str | None:
    """Derive a deterministic runtime identity from immutable source inputs."""

    values = {
        "base_image_digest": base_image_digest,
        "connector_source_sha256": connector_source_sha256,
        "fee_source_sha256": fee_source_sha256,
    }
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        return None
    return "sha256:" + sha256_canonical(values)


def compute_account_fingerprint(
    *,
    connector_name: str,
    environment: str,
    domain: str,
    account_type: str,
    subaccount_id: Any,
    public_owner: str,
) -> str | None:
    """Hash only the connector's public account identity.

    Derive's owner address is public account identity, not a signing secret,
    but it still must never be written to lifecycle evidence or diagnostics.
    The caller supplies it only in memory; this function returns a one-way
    fingerprint that is stable for the owner/subaccount/runtime domain.
    Private keys, API secrets, and decrypted credential blobs are deliberately
    not accepted as inputs.
    """

    if subaccount_id is None or public_owner is None:
        return None
    values = {
        "scheme": ACCOUNT_FINGERPRINT_SCHEME,
        "connector_name": str(connector_name or "").strip().lower(),
        "environment": str(environment or "").strip().lower(),
        "domain": str(domain or "").strip().lower(),
        "account_type": str(account_type or "").strip().lower(),
        "subaccount_id": str(subaccount_id).strip(),
        "public_owner": str(public_owner or "").strip().lower(),
    }
    if not all(values[key] for key in values if key != "scheme"):
        return None
    return "sha256:" + sha256_canonical(values)


@dataclass(frozen=True)
class LifecycleEvidence:
    """Immutable wrapper around one canonical lifecycle evidence document."""

    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> LifecycleEvidence:
        if not isinstance(payload, Mapping):
            raise TypeError("lifecycle evidence must be a mapping")
        return cls(_freeze(dict(payload)))

    @classmethod
    def from_json(cls, text: str) -> LifecycleEvidence:
        parsed = json.loads(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("lifecycle evidence JSON must contain an object")
        return cls.from_mapping(parsed)

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self.payload)

    def canonical_json(self) -> str:
        return canonical_json(self.as_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def evidence_id(self) -> str | None:
        value = self.payload.get("evidence_id")
        return str(value) if value not in (None, "") else None


@dataclass(frozen=True)
class LifecycleValidation:
    valid: bool
    blockers: tuple[str, ...] = ()
    checks: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    evidence_id: str | None = None
    evidence_sha256: str | None = None
    artifact_present: bool = False
    approved_sha256: str | None = APPROVED_LIFECYCLE_EVIDENCE_SHA256
    generated_at: str | None = None
    evidence_runtime_instance_id: str | None = None
    stage_a_status: str | None = None
    stage_b_status: str | None = None
    cleanup_status: str | None = None
    runtime_identity_match: bool = False
    account_identity_match: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "blockers": list(self.blockers),
            "checks": dict(self.checks),
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "artifact_present": self.artifact_present,
            "approved_sha256": self.approved_sha256,
            "generated_at": self.generated_at,
            "evidence_runtime_instance_id": self.evidence_runtime_instance_id,
            "stage_a_status": self.stage_a_status,
            "stage_b_status": self.stage_b_status,
            "cleanup_status": self.cleanup_status,
            "runtime_identity_match": self.runtime_identity_match,
            "account_identity_match": self.account_identity_match,
        }


def _walk_sensitive_keys(value: Any, prefix: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                found.append(path)
            found.extend(_walk_sensitive_keys(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_walk_sensitive_keys(child, f"{prefix}[{index}]"))
    return tuple(found)


def _decimal_equal(actual: Any, expected: str) -> bool:
    try:
        return Decimal(str(actual)) == Decimal(expected)
    except (InvalidOperation, TypeError, ValueError):
        return False


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _check_static_contract(contract: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "open_reduce_only": contract.get("open_reduce_only") is False,
        "close_reduce_only": contract.get("close_reduce_only") is True,
        "limit_tif": str(contract.get("limit_tif", "")).lower() == "gtc",
        "limit_maker_tif": str(contract.get("limit_maker_tif", "")).lower()
        == "post_only",
        "maker_fee_decimal": _decimal_equal(
            contract.get("maker_fee_decimal"), EXPECTED_STATIC_CONTRACT["maker_fee_decimal"]
        ),
        "taker_fee_decimal": _decimal_equal(
            contract.get("taker_fee_decimal"), EXPECTED_STATIC_CONTRACT["taker_fee_decimal"]
        ),
    }


def validate_lifecycle_evidence(
    evidence: LifecycleEvidence,
    *,
    expected_runtime: Mapping[str, Any] | None = None,
    expected_source: Mapping[str, Any] | None = None,
    expected_account: Mapping[str, Any] | None = None,
    expected_static_contract: Mapping[str, Any] | None = None,
    approved_sha256: str | None = APPROVED_LIFECYCLE_EVIDENCE_SHA256,
) -> LifecycleValidation:
    """Validate a complete artifact against independently supplied evidence."""

    payload = evidence.as_dict()
    blockers: list[str] = []
    checks: dict[str, bool] = {}

    checks["schema_version"] = payload.get("schema_version") == LIFECYCLE_EVIDENCE_SCHEMA_VERSION
    if not checks["schema_version"]:
        blockers.append("lifecycle_evidence_schema_invalid")

    checks["identity"] = all(
        payload.get(key) == expected for key, expected in EXPECTED_IDENTITY.items()
    )
    if not checks["identity"]:
        blockers.append("lifecycle_evidence_identity_mismatch")

    checks["evidence_id"] = bool(evidence.evidence_id)
    if not checks["evidence_id"]:
        blockers.append("lifecycle_evidence_id_missing")

    sensitive = _walk_sensitive_keys(payload)
    checks["no_secrets"] = not sensitive
    if sensitive:
        blockers.append("lifecycle_evidence_sensitive_field_present")

    source = _mapping(payload.get("source"))
    checks["source"] = bool(source.get("repository")) and bool(source.get("commit"))
    if expected_source is not None:
        checks["source"] = checks["source"] and all(
            source.get(key) == expected for key, expected in expected_source.items()
        )
    if not checks["source"]:
        blockers.append("lifecycle_evidence_source_mismatch")

    runtime = _mapping(payload.get("runtime"))
    checks["runtime"] = bool(
        runtime.get("runtime_contract_id") and runtime.get("runtime_image_ref")
    )
    if expected_runtime is not None:
        expected_reusable_runtime = {
            key: expected_runtime[key]
            for key in REUSABLE_RUNTIME_IDENTITY_KEYS
            if key in expected_runtime
        }
        checks["runtime"] = checks["runtime"] and all(
            runtime.get(key) == expected
            for key, expected in expected_reusable_runtime.items()
        )
    if not checks["runtime"]:
        blockers.append("lifecycle_evidence_runtime_mismatch")
    checks["runtime_instance_binding"] = bool(
        runtime.get("runtime_instance_id") and runtime.get("runtime_image_ref")
    )
    if not checks["runtime_instance_binding"]:
        blockers.append("lifecycle_evidence_runtime_instance_binding_missing")

    static_contract = _mapping(payload.get("static_contract"))
    static_checks = _check_static_contract(static_contract)
    checks.update({f"static_{key}": value for key, value in static_checks.items()})
    if expected_static_contract is not None:
        for key, expected in expected_static_contract.items():
            checks[f"static_matches_runtime_{key}"] = static_contract.get(key) == expected
    if not all(static_checks.values()) or any(
        key.startswith("static_matches_runtime_") and not value for key, value in checks.items()
    ):
        blockers.append("lifecycle_evidence_static_contract_mismatch")

    account = _mapping(payload.get("account"))
    checks["account_fingerprint"] = bool(account.get("account_fingerprint"))
    checks["account_clean_start"] = account.get("clean_at_start") is True
    if expected_account is not None:
        checks["account_matches_runtime"] = all(
            account.get(key) == expected for key, expected in expected_account.items()
        )
    else:
        # A static artifact must never be enough to assert identity.  The
        # caller has to supply the independently read account fingerprint.
        checks["account_matches_runtime"] = False
    if not all(
        checks[key]
        for key in ("account_fingerprint", "account_clean_start", "account_matches_runtime")
    ):
        blockers.append("lifecycle_evidence_account_mismatch_or_dirty")

    stage_a = _mapping(payload.get("stage_a"))
    stage_b = _mapping(payload.get("stage_b"))
    cleanup = _mapping(payload.get("cleanup"))
    checks["stage_a_passed"] = str(stage_a.get("status", "")).upper() == "PASS"
    checks["stage_b_passed"] = str(stage_b.get("status", "")).upper() == "PASS"
    checks["cleanup_passed"] = (
        str(cleanup.get("status", "")).upper() == "PASS"
        and cleanup.get("position_zero") is True
        and cleanup.get("active_orders") == 0
        and cleanup.get("managed_executors") == 0
        and cleanup.get("unmanaged_executors") == 0
    )
    if not checks["stage_a_passed"]:
        blockers.append("lifecycle_stage_a_not_passed")
    if not checks["stage_b_passed"]:
        blockers.append("lifecycle_stage_b_not_passed")
    if not checks["cleanup_passed"]:
        blockers.append("lifecycle_cleanup_not_clean")

    verdict = _mapping(payload.get("verdict"))
    checks["verdict_passed"] = str(verdict.get("status", "")).upper() == "PASS"
    if not checks["verdict_passed"]:
        blockers.append("lifecycle_evidence_verdict_not_pass")

    evidence_hash = evidence.sha256()
    checks["approved_sha256"] = bool(approved_sha256) and evidence_hash == approved_sha256
    if not checks["approved_sha256"]:
        blockers.append("lifecycle_evidence_sha256_not_approved")

    return LifecycleValidation(
        valid=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        checks=MappingProxyType(dict(checks)),
        evidence_id=evidence.evidence_id,
        evidence_sha256=evidence_hash,
        artifact_present=True,
        approved_sha256=approved_sha256,
        generated_at=str(payload.get("generated_at", payload.get("created_at")))
        if payload.get("generated_at", payload.get("created_at")) is not None
        else None,
        evidence_runtime_instance_id=(
            str(runtime.get("runtime_instance_id"))
            if runtime.get("runtime_instance_id") is not None
            else None
        ),
        stage_a_status=str(stage_a.get("status")) if stage_a.get("status") is not None else None,
        stage_b_status=str(stage_b.get("status")) if stage_b.get("status") is not None else None,
        cleanup_status=str(cleanup.get("status")) if cleanup.get("status") is not None else None,
        runtime_identity_match=bool(
            checks.get("runtime") and checks.get("runtime_instance_binding")
        ),
        account_identity_match=bool(checks.get("account_matches_runtime")),
    )


def validate_promoted_lifecycle_evidence(
    path: str | Path = PROMOTED_LIFECYCLE_EVIDENCE_PATH,
    **kwargs: Any,
) -> LifecycleValidation:
    """Read the fixed promoted artifact and validate it fail-closed."""

    artifact = Path(path)
    if not artifact.is_file():
        return LifecycleValidation(
            valid=False,
            blockers=("lifecycle_evidence_missing",),
            checks=MappingProxyType({"artifact_present": False}),
            artifact_present=False,
            approved_sha256=kwargs.get("approved_sha256", APPROVED_LIFECYCLE_EVIDENCE_SHA256),
        )
    try:
        evidence = LifecycleEvidence.from_json(artifact.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return LifecycleValidation(
            valid=False,
            blockers=(f"lifecycle_evidence_unreadable:{type(exc).__name__}",),
            checks=MappingProxyType({"artifact_present": True}),
            artifact_present=True,
            approved_sha256=kwargs.get("approved_sha256", APPROVED_LIFECYCLE_EVIDENCE_SHA256),
        )
    return validate_lifecycle_evidence(evidence, **kwargs)


def load_promoted_lifecycle_evidence(
    path: str | Path = PROMOTED_LIFECYCLE_EVIDENCE_PATH,
) -> LifecycleEvidence | None:
    artifact = Path(path)
    if not artifact.is_file():
        return None
    try:
        return LifecycleEvidence.from_json(artifact.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_lifecycle_evidence(payload: Mapping[str, Any]) -> LifecycleEvidence:
    """Construct an immutable evidence document without adding permissions."""

    return LifecycleEvidence.from_mapping(payload)


__all__ = [
    "APPROVED_LIFECYCLE_EVIDENCE_SHA256",
    "ACCOUNT_FINGERPRINT_SCHEME",
    "EXPECTED_IDENTITY",
    "EXPECTED_STATIC_CONTRACT",
    "HOST_PROMOTED_LIFECYCLE_EVIDENCE_PATH",
    "LIFECYCLE_EVIDENCE_SCHEMA_VERSION",
    "LifecycleEvidence",
    "LifecycleValidation",
    "PROMOTED_LIFECYCLE_EVIDENCE_PATH",
    "REUSABLE_RUNTIME_IDENTITY_KEYS",
    "build_lifecycle_evidence",
    "canonical_json",
    "compute_runtime_contract_id",
    "compute_account_fingerprint",
    "load_promoted_lifecycle_evidence",
    "sha256_canonical",
    "validate_lifecycle_evidence",
    "validate_promoted_lifecycle_evidence",
]
