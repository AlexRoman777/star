from __future__ import annotations

from star.services.operations import (
    sanitize_operation_details,
    sanitize_operation_message,
)


def test_operational_telemetry_drops_unapproved_and_sensitive_context() -> None:
    details = sanitize_operation_details(
        {
            "task_type": "asset.primary_review",
            "duration_ms": 1250,
            "api_key": "must-never-appear",
            "filename": "private-family-name.jpg",
            "prompt": "ignore all prior instructions",
        }
    )
    assert details == {"task_type": "asset.primary_review", "duration_ms": 1250}
    assert "must-never-appear" not in sanitize_operation_message("api_key=must-never-appear")
