from __future__ import annotations

import pytest

from star.immich.client import ImmichIdentity
from star.services.connections import (
    READ_RECOMMENDED_PERMISSIONS,
    READ_REQUIRED_PERMISSIONS,
    validate_read_identity,
    write_policy_for_permissions,
)


def identity(version: str, permissions: set[str]) -> ImmichIdentity:
    return ImmichIdentity(
        user_id="user-1",
        user_name="Camera",
        is_admin=False,
        api_key_id="key-1",
        api_key_name="Star",
        permissions=sorted(permissions),
        server_version=version,
    )


@pytest.mark.parametrize("version", ["2.7.5", "3.0.3"])
def test_read_key_supports_immich_two_and_three(version: str) -> None:
    permissions, missing_recommended = validate_read_identity(
        identity(version, set(READ_REQUIRED_PERMISSIONS))
    )
    assert set(permissions) == READ_REQUIRED_PERMISSIONS
    assert set(missing_recommended) == READ_RECOMMENDED_PERMISSIONS


def test_read_key_reports_exact_missing_permissions_and_rejects_all() -> None:
    with pytest.raises(ValueError, match=r"asset\.view"):
        validate_read_identity(identity("2.7.5", set(READ_REQUIRED_PERMISSIONS) - {"asset.view"}))
    with pytest.raises(ValueError, match="least-privilege"):
        validate_read_identity(identity("2.7.5", {"all"}))


def test_one_key_enables_only_the_write_operations_its_permissions_support() -> None:
    read_only = write_policy_for_permissions(sorted(READ_REQUIRED_PERMISSIONS))
    assert read_only == {
        "enabled": False,
        "allowed_operations": [],
        "requires_human_approval": True,
    }

    asset_write = write_policy_for_permissions(sorted(READ_REQUIRED_PERMISSIONS | {"asset.update"}))
    assert asset_write["allowed_operations"] == ["description", "rating"]

    all_selected = write_policy_for_permissions(
        sorted(READ_REQUIRED_PERMISSIONS | {"asset.update", "tag.create", "tag.asset"})
    )
    assert all_selected["allowed_operations"] == ["description", "rating", "tags"]
