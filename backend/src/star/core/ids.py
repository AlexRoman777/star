from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate a sortable UUIDv7 without requiring Python 3.14."""
    timestamp_ms = time.time_ns() // 1_000_000
    random_bytes = bytearray(os.urandom(10))
    random_bytes[0] = (random_bytes[0] & 0x0F) | 0x70
    random_bytes[2] = (random_bytes[2] & 0x3F) | 0x80
    value = timestamp_ms.to_bytes(6, "big") + bytes(random_bytes)
    return uuid.UUID(bytes=value)
