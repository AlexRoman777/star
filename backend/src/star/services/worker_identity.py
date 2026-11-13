from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _decode_urlsafe(value: str, *, expected_bytes: int) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("device identity contains invalid base64") from exc
    if len(decoded) != expected_bytes:
        raise ValueError("device identity has an invalid length")
    return decoded


def automatic_enrollment_message(
    *,
    heartbeat: dict[str, Any],
    device_public_key: str,
    issued_at: datetime,
    nonce: str,
) -> bytes:
    document = {
        "device_public_key": device_public_key,
        "heartbeat": heartbeat,
        "issued_at": issued_at.isoformat(),
        "nonce": nonce,
        "purpose": "star-local-worker-enrollment-v1",
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_automatic_enrollment_signature(
    *,
    heartbeat: dict[str, Any],
    device_public_key: str,
    issued_at: datetime,
    nonce: str,
    signature: str,
) -> str:
    public_key_bytes = _decode_urlsafe(device_public_key, expected_bytes=32)
    signature_bytes = _decode_urlsafe(signature, expected_bytes=64)
    message = automatic_enrollment_message(
        heartbeat=heartbeat,
        device_public_key=device_public_key,
        issued_at=issued_at,
        nonce=nonce,
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, message)
    except InvalidSignature as exc:
        raise ValueError("device enrollment signature is invalid") from exc
    return "ed25519:" + hashlib.sha256(public_key_bytes).hexdigest()
