from decimal import Decimal
from pathlib import Path

from derive_options_adaptive_grid.connector_contract import (
    _runtime_instance_id_from_mountinfo,
    normalized_fee_observation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime/derive-options-grid-hummingbot"


def test_fee_schema_values_are_decimal_fractions_without_double_conversion():
    percent_point_value = normalized_fee_observation("0.01")
    already_normalized = normalized_fee_observation("0.0001")

    assert percent_point_value["raw_unit"] == "decimal_fraction"
    assert percent_point_value["normalized_decimal_rate"] == Decimal("0.01")
    assert already_normalized["normalized_decimal_rate"] == Decimal("0.0001")


def test_runtime_image_is_pinned_and_contract_patch_is_fail_closed():
    dockerfile = (RUNTIME_ROOT / "Dockerfile").read_text()
    patch_script = (RUNTIME_ROOT / "patch_derive_connector.py").read_text()

    assert (
        "hummingbot/hummingbot@sha256:632d2b07aa156b761310f2f7258a78c9660a1c28b6df4b33874e09a0c7d06c85"
        in dockerfile
    )
    assert "hummingbot/hummingbot:latest" not in dockerfile
    assert "BASE_ORDER_SOURCE_SHA256" in patch_script
    assert "BASE_UTILS_SOURCE_SHA256" in patch_script
    assert '"post_only"' in patch_script
    assert "position_action == PositionAction.CLOSE" in patch_script


def test_runtime_verification_is_transport_stub_only():
    verifier = (RUNTIME_ROOT / "verify_runtime_contract.py").read_text()
    assert "_api_post" not in verifier
    assert "transport stub only" in verifier
    assert "contract_verified" in verifier


def test_runtime_instance_identity_uses_a_one_way_container_overlay_digest():
    mountinfo = (
        "540 435 0:116 / / rw,relatime - overlay overlay "
        "rw,lowerdir=/layers/a,upperdir=/containers/unique-upper,workdir=/containers/work"
    )

    instance_id = _runtime_instance_id_from_mountinfo(mountinfo)

    assert instance_id is not None
    assert instance_id.startswith("sha256:")
    assert "unique-upper" not in instance_id
    assert instance_id == _runtime_instance_id_from_mountinfo(mountinfo)
