from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from star.agent_main import _identity
from star.services.worker_identity import (
    automatic_enrollment_message,
    verify_automatic_enrollment_signature,
)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def test_signed_worker_identity_is_bound_to_heartbeat() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = _encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    issued_at = datetime.now(UTC)
    heartbeat = {"hostname_label": "mac", "maximum_concurrency": 3}
    message = automatic_enrollment_message(
        heartbeat=heartbeat,
        device_public_key=public_key,
        issued_at=issued_at,
        nonce="n" * 32,
    )
    subject = verify_automatic_enrollment_signature(
        heartbeat=heartbeat,
        device_public_key=public_key,
        issued_at=issued_at,
        nonce="n" * 32,
        signature=_encode(private_key.sign(message)),
    )
    assert subject.startswith("ed25519:")

    with pytest.raises(ValueError, match="signature"):
        verify_automatic_enrollment_signature(
            heartbeat={**heartbeat, "maximum_concurrency": 30},
            device_public_key=public_key,
            issued_at=issued_at,
            nonce="n" * 32,
            signature=_encode(private_key.sign(message)),
        )


def test_worker_identity_advertises_release_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STAR_WORKER_RELEASE_VERSION", "v53")
    monkeypatch.setenv("STAR_BUILD_GIT_SHA", "a" * 40)
    monkeypatch.setenv("STAR_BUILD_TREE_STATE", "dirty")
    monkeypatch.setenv("STAR_BUILD_SOURCE_FINGERPRINT", "b" * 64)

    identity = _identity(
        "nvidia-photo-review",
        {"small-vlm": True},
        storage_path=tmp_path,
    )

    assert identity["release_version"] == "v53"
    assert identity["build_git_sha"] == "a" * 40
    assert identity["build_tree_state"] == "dirty"
    assert identity["build_source_fingerprint"] == "b" * 64


def test_worker_identity_advertises_physical_machine_group(tmp_path) -> None:
    identity = _identity(
        "apple-vision-legacy",
        {"apple-vision": True},
        machine_group="apple-main",
        model_runtime_routes={
            "qwen3-vl:8b-instruct-q4_K_M": "ollama",
            "qwen3.8:27b": "mlx-vlm",
        },
        storage_path=tmp_path,
        runtime_details={"resident_model_keys": [], "model_idle_ttl_seconds": 0},
    )

    assert identity["machine_group"] == "apple-main"
    assert identity["model_runtime_routes"] == {
        "qwen3-vl:8b-instruct-q4_K_M": "ollama",
        "qwen3.8:27b": "mlx-vlm",
    }
    assert identity["resident_model_keys"] == []
    assert identity["model_idle_ttl_seconds"] == 0


def test_worker_identity_advertises_protocol_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STAR_WORKER_PROTOCOL_VERSION", "99")

    identity = _identity(
        "nvidia-photo-review",
        {"small-vlm": True},
        storage_path=tmp_path,
    )

    assert identity["worker_protocol_version"] == 2
    assert identity["worker_contract_hash"] != "unknown"
    assert identity["task_contract_revisions"]["asset.primary_review"] == 4
