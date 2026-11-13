"""Counts of what the media relay asked Immich for, kept in this process.

They were Prometheus counters; Star keeps no external observability now, so
they are plain counters the relay can still increment and read.
"""

from __future__ import annotations


class RelayCounter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def labels(self, **_labels: object) -> RelayCounter:
        return self

    def inc(self, amount: int = 1) -> None:
        self.value += amount


IMMICH_FULLSIZE_REQUESTS = RelayCounter()
IMMICH_PREVIEW_BYTES = RelayCounter()
IMMICH_PREVIEW_FAILURES = RelayCounter()
IMMICH_PREVIEW_REQUESTS = RelayCounter()
IMMICH_REDIRECTS_REJECTED = RelayCounter()
IMMICH_THUMBNAIL_REQUESTS = RelayCounter()
WORKER_MEDIA_STREAM_FAILURES = RelayCounter()

__all__ = [
    "IMMICH_FULLSIZE_REQUESTS",
    "IMMICH_PREVIEW_BYTES",
    "IMMICH_PREVIEW_FAILURES",
    "IMMICH_PREVIEW_REQUESTS",
    "IMMICH_REDIRECTS_REJECTED",
    "IMMICH_THUMBNAIL_REQUESTS",
    "WORKER_MEDIA_STREAM_FAILURES",
    "RelayCounter",
]
