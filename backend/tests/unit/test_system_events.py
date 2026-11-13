from __future__ import annotations

import json

import pytest

from star.services.system_events import (
    SYSTEM_EVENT_TYPES,
    SystemChange,
    format_system_sse_event,
    parse_system_event_payload,
)


@pytest.mark.parametrize("event_type", sorted(SYSTEM_EVENT_TYPES))
def test_system_event_payload_accepts_only_allowlisted_domains(event_type: str) -> None:
    assert parse_system_event_payload(event_type) == SystemChange(event_type=event_type)


def test_system_event_payload_accepts_safe_target_identifiers() -> None:
    change = parse_system_event_payload(
        '{"event":"asset.changed","table":"analysis_runs","entity_id":"run-1","asset_id":"asset-2"}'
    )
    assert change == SystemChange(
        event_type="asset.changed",
        table="analysis_runs",
        entity_id="run-1",
        asset_id="asset-2",
    )


def test_system_event_payload_rejects_untrusted_or_oversized_context() -> None:
    assert parse_system_event_payload("task.changed\napi_key=private") is None
    assert parse_system_event_payload('{"event":"task.changed","filename":"private.jpg"}') is None
    assert parse_system_event_payload("") is None


def test_system_sse_event_contains_only_safe_routing_context() -> None:
    encoded = format_system_sse_event(
        SystemChange(
            event_type="task.changed",
            table="tasks",
            entity_id="task-1",
            asset_id="asset-1",
        )
    )
    data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))

    assert "event: task.changed" in encoded
    assert payload["event"] == "task.changed"
    assert payload["table"] == "tasks"
    assert payload["entity_id"] == "task-1"
    assert payload["asset_id"] == "asset-1"
    assert set(payload) == {"event", "emitted_at", "table", "entity_id", "asset_id"}


def test_system_sse_event_rejects_unknown_types() -> None:
    with pytest.raises(ValueError, match="unsupported system event type"):
        format_system_sse_event("asset.private.changed")
