from pathlib import Path

import pytest

from derive_options_adaptive_grid.connector_contract import account_binding_from_connector
from derive_options_adaptive_grid.lifecycle_evidence import (
    ACCOUNT_FINGERPRINT_SCHEME,
    EXPECTED_STATIC_CONTRACT,
    LifecycleEvidence,
    compute_account_fingerprint,
    compute_runtime_contract_id,
    validate_lifecycle_evidence,
    validate_promoted_lifecycle_evidence,
)

RUNTIME = {
    "base_image_digest": "sha256:image",
    "runtime_contract_id": compute_runtime_contract_id(
        "sha256:image", "sha256:connector", "sha256:fees"
    ),
    "connector_source_sha256": "sha256:connector",
    "fee_source_sha256": "sha256:fees",
    "runtime_instance_id": "container-1",
    "runtime_image_ref": "local/derive-options-grid-hummingbot:connector-v2",
}
ACCOUNT = {"account_fingerprint": "sha256:account", "clean_at_start": True}


def _evidence(**overrides):
    payload = {
        "schema_version": 1,
        "evidence_id": "lifecycle-1",
        "created_at": "2026-09-21T00:00:00Z",
        "environment": "mainnet",
        "connector_name": "derive_perpetual",
        "trading_pair": "SOL-USDC",
        "exchange_instrument": "SOL-PERP",
        "position_mode": "ONEWAY",
        "leverage": 1,
        "source": {"repository": "doi-doi/Derive-Options-Adaptive-Grid", "commit": "reviewed"},
        "runtime": dict(RUNTIME),
        "account": dict(ACCOUNT),
        "static_contract": dict(EXPECTED_STATIC_CONTRACT),
        "stage_a": {
            "status": "PASS",
            "position_action": "OPEN",
            "reduce_only": False,
            "order_type": "LIMIT_MAKER",
            "time_in_force": "post_only",
            "post_only_requested": True,
            "resting_verified": True,
            "native_order_id": "native-a",
            "fill_count": 0,
            "cancel_acknowledged": True,
            "active_orders": 0,
            "position_after": "0",
        },
        "stage_b": {
            "status": "PASS",
            "position_action": "CLOSE",
            "reduce_only": True,
            "order_type": "LIMIT_MAKER",
            "time_in_force": "post_only",
            "close_reduce_only_requested": True,
            "close_native_order_id": "native-b",
            "fill_count": 1,
            "position_before": "1",
            "position_after": "0",
            "oversized_close_tested": False,
        },
        "cleanup": {
            "status": "PASS",
            "position_zero": True,
            "active_orders": 0,
            "managed_executors": 0,
            "unmanaged_executors": 0,
        },
        "verdict": {"status": "PASS"},
    }
    payload.update(overrides)
    return LifecycleEvidence.from_mapping(payload)


def test_canonical_hash_is_stable_and_payload_is_immutable():
    first = _evidence()
    second = LifecycleEvidence.from_mapping(
        {key: value for key, value in reversed(list(first.as_dict().items()))}
    )
    assert first.canonical_json() == second.canonical_json()
    assert first.sha256() == second.sha256()
    with pytest.raises(TypeError):
        first.payload["environment"] = "testnet"


def test_complete_evidence_requires_independent_runtime_and_account_match():
    evidence = _evidence()
    validation = validate_lifecycle_evidence(
        evidence,
        expected_runtime=RUNTIME,
        expected_source={"repository": "doi-doi/Derive-Options-Adaptive-Grid"},
        expected_account=ACCOUNT,
        expected_static_contract=EXPECTED_STATIC_CONTRACT,
        approved_sha256=evidence.sha256(),
    )
    assert validation.valid is True
    assert validation.blockers == ()


def test_unapproved_or_missing_runtime_proof_fails_closed():
    evidence = _evidence()
    unapproved = validate_lifecycle_evidence(
        evidence,
        expected_runtime=RUNTIME,
        expected_account=ACCOUNT,
        approved_sha256=None,
    )
    assert unapproved.valid is False
    assert "lifecycle_evidence_sha256_not_approved" in unapproved.blockers

    missing = validate_promoted_lifecycle_evidence(Path("/definitely/missing/evidence.json"))
    assert missing.valid is False
    assert missing.blockers == ("lifecycle_evidence_missing",)


def test_promoted_evidence_survives_fresh_controller_instance():
    evidence = _evidence()
    validation = validate_lifecycle_evidence(
        evidence,
        expected_runtime={**RUNTIME, "runtime_instance_id": "controller-2"},
        expected_account=ACCOUNT,
        approved_sha256=evidence.sha256(),
    )
    assert validation.valid is True
    assert validation.evidence_runtime_instance_id == "container-1"


def test_promoted_evidence_still_requires_same_reusable_runtime():
    evidence = _evidence()
    validation = validate_lifecycle_evidence(
        evidence,
        expected_runtime={**RUNTIME, "runtime_image_ref": "different-image"},
        expected_account=ACCOUNT,
        approved_sha256=evidence.sha256(),
    )
    assert validation.valid is False
    assert "lifecycle_evidence_runtime_mismatch" in validation.blockers


def test_wrong_source_fee_runtime_account_and_cleanup_are_reported():
    evidence = _evidence(
        runtime={**RUNTIME, "runtime_contract_id": "sha256:wrong"},
        account={"account_fingerprint": "sha256:other", "clean_at_start": False},
        static_contract={**EXPECTED_STATIC_CONTRACT, "close_reduce_only": False},
        cleanup={
            "status": "FAIL",
            "position_zero": False,
            "active_orders": 1,
            "managed_executors": 0,
            "unmanaged_executors": 0,
        },
    )
    validation = validate_lifecycle_evidence(
        evidence,
        expected_runtime=RUNTIME,
        expected_source={"repository": "other/repository"},
        expected_account=ACCOUNT,
        expected_static_contract=EXPECTED_STATIC_CONTRACT,
        approved_sha256=evidence.sha256(),
    )
    assert validation.valid is False
    assert "lifecycle_evidence_source_mismatch" in validation.blockers
    assert "lifecycle_evidence_runtime_mismatch" in validation.blockers
    assert "lifecycle_evidence_account_mismatch_or_dirty" in validation.blockers
    assert "lifecycle_evidence_static_contract_mismatch" in validation.blockers
    assert "lifecycle_cleanup_not_clean" in validation.blockers


def test_sensitive_fields_are_rejected():
    evidence = _evidence(account={**ACCOUNT, "api_secret": "never"})
    validation = validate_lifecycle_evidence(
        evidence,
        expected_runtime=RUNTIME,
        expected_account={**ACCOUNT, "api_secret": "never"},
        approved_sha256=evidence.sha256(),
    )
    assert "lifecycle_evidence_sensitive_field_present" in validation.blockers


def test_account_fingerprint_is_one_way_and_stable_without_recording_owner():
    first = compute_account_fingerprint(
        connector_name="derive_perpetual",
        environment="mainnet",
        domain="derive_perpetual",
        account_type="trader",
        subaccount_id=7,
        public_owner="0xpublic-owner",
    )
    second = compute_account_fingerprint(
        connector_name="derive_perpetual",
        environment="mainnet",
        domain="derive_perpetual",
        account_type="trader",
        subaccount_id=7,
        public_owner="0xpublic-owner",
    )
    assert first == second
    assert first.startswith("sha256:")
    assert ACCOUNT_FINGERPRINT_SCHEME not in first
    assert "public-owner" not in first


def test_connector_account_binding_uses_public_identity_only():
    class Connector:
        derive_perpetual_api_key = "public-owner"
        _sub_id = 7
        _account_type = "trader"
        domain = "derive_perpetual"
        name = "derive_perpetual"
        derive_perpetual_secret_key = "must-not-be-read"

    binding = account_binding_from_connector(Connector())
    assert binding is not None
    assert binding["account_fingerprint"].startswith("sha256:")
    assert "public-owner" not in binding["account_fingerprint"]
    assert "secret" not in binding
