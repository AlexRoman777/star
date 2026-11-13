from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

from star.core.config import Settings
from star.db.base import utc_now

SYSTEM_EVENT_CHANNEL = "star_system_changes"
SYSTEM_EVENT_TYPES = frozenset(
    {
        "batch.changed",
        "coordinator.changed",
        "asset.changed",
        "model.changed",
        "immich.changed",
        "audit.changed",
        "collection.changed",
        "storage.changed",
        "program.changed",
        "task.changed",
        "worker.changed",
        "writeback.changed",
    }
)
SYSTEM_EVENT_TABLES = frozenset(
    {
        "tasks",
        "asset_review_cycles",
        "workers",
        "library_programs",
        "deep_review_backfills",
        "processing_batches",
        "analysis_runs",
        "service_heartbeats",
        "writeback_batches",
        "writeback_operations",
        "assets",
        "asset_revisions",
        "asset_curation_snapshots",
        "asset_immich_contexts",
        "asset_face_references",
        "album_references",
        "album_asset_references",
        "tag_references",
        "collection_context_snapshots",
        "collection_groups",
        "collection_group_members",
        "model_versions",
        "model_packages",
        "model_deployments",
        "immich_instances",
        "immich_connections",
        "audit_events",
        "audit_checkpoints",
        "storage_inventories",
    }
)
SYSTEM_EVENT_IDENTIFIER_KEYS = ("entity_id", "asset_id", "connection_id", "batch_id", "group_id")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class SystemChange:
    event_type: str
    table: str | None = None
    entity_id: str | None = None
    asset_id: str | None = None
    connection_id: str | None = None
    batch_id: str | None = None
    group_id: str | None = None


class SystemEventTransportClosed(RuntimeError):
    """The dedicated PostgreSQL notification connection ended."""


def parse_system_event_payload(payload: str) -> SystemChange | None:
    if payload in SYSTEM_EVENT_TYPES:
        return SystemChange(event_type=payload)
    if not payload or len(payload) > 1_024:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    allowed_keys = {"event", "table", *SYSTEM_EVENT_IDENTIFIER_KEYS}
    if not isinstance(data, dict) or set(data) - allowed_keys:
        return None
    event_type = data.get("event")
    table = data.get("table")
    if event_type not in SYSTEM_EVENT_TYPES:
        return None
    if table is not None and table not in SYSTEM_EVENT_TABLES:
        return None
    identifiers: dict[str, str | None] = {}
    for key in SYSTEM_EVENT_IDENTIFIER_KEYS:
        value = data.get(key)
        if value is not None and (
            not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            return None
        identifiers[key] = value
    return SystemChange(event_type=event_type, table=table, **identifiers)


def format_system_sse_event(change: SystemChange | str) -> str:
    value = SystemChange(change) if isinstance(change, str) else change
    event_type = value.event_type
    if event_type != "system.ready" and event_type not in SYSTEM_EVENT_TYPES:
        raise ValueError("unsupported system event type")
    payload = {"event": event_type, "emitted_at": utc_now().isoformat()}
    for key in ("table", *SYSTEM_EVENT_IDENTIFIER_KEYS):
        identifier = getattr(value, key)
        if identifier is not None:
            payload[key] = identifier
    encoded = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_type}\ndata: {encoded}\n\n"


def _postgres_dsn(settings: Settings) -> str | None:
    url = make_url(settings.async_database_url)
    if url.get_backend_name() != "postgresql":
        return None
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


class _ChangeAccumulator:
    """Collapse repeated row notifications into one event per affected domain."""

    def __init__(self) -> None:
        self._changes: dict[tuple[str, ...], SystemChange] = {}
        self._ready = asyncio.Event()
        self._terminated = False

    def add(self, payload: str) -> None:
        change = parse_system_event_payload(payload)
        if change is None:
            return
        key = tuple(
            value or ""
            for value in (
                change.event_type,
                change.table,
                change.entity_id,
                change.asset_id,
                change.connection_id,
                change.batch_id,
                change.group_id,
            )
        )
        self._changes[key] = change
        self._ready.set()

    def terminate(self) -> None:
        self._terminated = True
        self._ready.set()

    async def take(self) -> Collection[SystemChange]:
        await self._ready.wait()
        if self._terminated:
            raise SystemEventTransportClosed("PostgreSQL notification connection closed")
        changes = tuple(self._changes.values())
        self._changes.clear()
        self._ready.clear()
        return changes


async def stream_system_event_types(
    settings: Settings,
    *,
    heartbeat_seconds: float,
) -> AsyncIterator[SystemChange | None]:
    """Yield coalesced committed PostgreSQL changes and periodic keepalive markers."""

    dsn = _postgres_dsn(settings)
    if dsn is None:
        # SQLite is used only by local tests and lightweight development. Keep the
        # transport alive there; production PostgreSQL uses transaction-aware NOTIFY.
        while True:
            await asyncio.sleep(heartbeat_seconds)
            yield None

    accumulator = _ChangeAccumulator()
    connection = await asyncpg.connect(dsn=dsn)

    def notification_listener(
        _connection: asyncpg.Connection,
        _process_id: int,
        _channel: str,
        payload: str,
    ) -> None:
        accumulator.add(payload)

    def termination_listener(_connection: asyncpg.Connection) -> None:
        accumulator.terminate()

    await connection.add_listener(SYSTEM_EVENT_CHANNEL, notification_listener)
    connection.add_termination_listener(termination_listener)
    try:
        while True:
            try:
                changes = await asyncio.wait_for(
                    accumulator.take(),
                    timeout=heartbeat_seconds,
                )
            except TimeoutError:
                if connection.is_closed():
                    raise SystemEventTransportClosed(
                        "PostgreSQL notification connection closed"
                    ) from None
                yield None
                continue
            for event_type in changes:
                yield event_type
    finally:
        if not connection.is_closed():
            await connection.remove_listener(SYSTEM_EVENT_CHANNEL, notification_listener)
            connection.remove_termination_listener(termination_listener)
            await connection.close()
