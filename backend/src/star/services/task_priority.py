from __future__ import annotations

from typing import Any

USER_REQUEST_PRIORITY = 1
USER_QUEUE_ORIGINS = frozenset({"manual", "user"})
FULL_PIPELINE_REQUEST_REASONS = frozenset(
    {
        "complete_reanalysis",
        "human_rating_validation",
        "immich_source_recovery",
        "owner_nsfw_validation",
    }
)


def is_user_requested(target_ref: object) -> bool:
    return isinstance(target_ref, dict) and target_ref.get("queue_origin") in USER_QUEUE_ORIGINS


def user_request_target(target_ref: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        **target_ref,
        "queue_origin": "user",
        "user_request_reason": reason,
    }


def requires_full_user_pipeline(target_ref: object) -> bool:
    return (
        is_user_requested(target_ref)
        and isinstance(target_ref, dict)
        and target_ref.get("user_request_reason") in FULL_PIPELINE_REQUEST_REASONS
    )
