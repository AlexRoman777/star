from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionSafetyClass(StrEnum):
    PREVIEW = "preview"
    AUTHENTICATION = "authentication"
    OWNER_CONFIGURATION = "owner_configuration"
    DURABLE_OPERATION = "durable_operation"
    LOCAL_DESTRUCTIVE = "local_destructive"
    WORKER_PROTOCOL = "worker_protocol"
    IMMICH_CONNECTION = "immich_connection"
    IMMICH_WRITEBACK = "immich_writeback"


@dataclass(frozen=True)
class ActionSafetyProfile:
    authority: str
    replay: str
    concurrency: str
    reset_behavior: str
    external_effect: str
    immich_metadata_write: bool = False
    immich_media_delete: bool = False


ACTION_SAFETY_PROFILES: dict[ActionSafetyClass, ActionSafetyProfile] = {
    ActionSafetyClass.PREVIEW: ActionSafetyProfile(
        authority="authenticated owner or explicitly public authentication ceremony",
        replay="read-only or bounded expiring challenge",
        concurrency="no durable work ownership transition",
        reset_behavior="cannot repopulate a reset photo generation",
        external_effect="none",
    ),
    ActionSafetyClass.AUTHENTICATION: ActionSafetyProfile(
        authority="password, bootstrap secret, or active session",
        replay="single-use challenge or revocable bounded session",
        concurrency="credential identity and challenge constraints",
        reset_behavior="account and login survive photo-library reset",
        external_effect="none",
    ),
    ActionSafetyClass.OWNER_CONFIGURATION: ActionSafetyProfile(
        authority="authenticated administrator or curator",
        replay="optimistic revision, target lock, or naturally idempotent replacement",
        concurrency="target row or catalog lock with one canonical lock order",
        reset_behavior="must not create obsolete-generation photo work",
        external_effect="none",
    ),
    ActionSafetyClass.DURABLE_OPERATION: ActionSafetyProfile(
        authority="authenticated owner",
        replay="idempotency key, singleton operation lock, or active-run reuse",
        concurrency="durable run/task ownership and bounded reconciliation",
        reset_behavior="generation-bound work is cancelled or rejected after reset",
        external_effect="none",
    ),
    ActionSafetyClass.LOCAL_DESTRUCTIVE: ActionSafetyProfile(
        authority="authenticated administrator with explicit target or confirmation",
        replay="terminal/idempotent outcome or stale confirmation rejection",
        concurrency="exclusive reset or locked target rows",
        reset_behavior="is itself a bounded local reset/removal action",
        external_effect="never deletes or trashes Immich media",
    ),
    ActionSafetyClass.WORKER_PROTOCOL: ActionSafetyProfile(
        authority="persistent worker credential and allowed route/data class",
        replay="lease identity plus result digest or single-use signed enrollment nonce",
        concurrency="worker/task row locks and generation/revision validation",
        reset_behavior="shared reset barrier rejects obsolete work",
        external_effect="none; local inference and evidence only",
    ),
    ActionSafetyClass.IMMICH_CONNECTION: ActionSafetyProfile(
        authority="authenticated administrator and encrypted least-privilege credential",
        replay="stable connection identity, checkpoint replay, or locked credential rotation",
        concurrency="connection/instance ownership locks and idempotent source upserts",
        reset_behavior="source identity survives; stale photo writes are generation-guarded",
        external_effect="local Immich reads and sync-checkpoint acknowledgement only",
    ),
    ActionSafetyClass.IMMICH_WRITEBACK: ActionSafetyProfile(
        authority="reauthenticated administrator approving one immutable preview digest",
        replay="compare-before-write operation ledger with idempotent recovery",
        concurrency="locked immutable batch and one execution lease",
        reset_behavior="prepared batches are invalidated by local reset",
        external_effect="explicit approved Immich metadata update; never media deletion",
        immich_metadata_write=True,
    ),
}


def _classified(
    safety_class: ActionSafetyClass,
    *endpoint_names: str,
) -> dict[str, ActionSafetyClass]:
    return dict.fromkeys(endpoint_names, safety_class)


ACTION_SAFETY_BY_ENDPOINT: dict[str, ActionSafetyClass] = {
    **_classified(
        ActionSafetyClass.PREVIEW,
        "preview_backfill",
        "preview_reanalysis",
        "preview_library_reset",
    ),
    **_classified(
        ActionSafetyClass.AUTHENTICATION,
        "initial_password_setup",
        "local_password_login",
        "create_audit_token",
        "revoke_audit_token",
    ),
    **_classified(
        ActionSafetyClass.OWNER_CONFIGURATION,
        "put_policy",
        "change_prompt_revision_state",
        "put_review_prompt",
        "reset_review_prompt",
        "update_asset_human_rating",
        "refresh_asset_immich_context",
        "update_asset_curation",
        "change_library_stage_state",
        "put_library_program_schedule",
        "put_library_program_policy",
        "put_library_inference_settings",
        "put_library_writeback_policy",
        "change_library_program_state",
        "clear_library_program_queue",
        "register_model",
        "record_benchmark",
        "register_model_package",
        "clear_operation_history",
        "create_preset",
        "update_preset",
        "delete_preset",
        # Recheck apply: records proposal-only ``recheck`` ratings for the owner's
        # chosen policy; idempotent per run and policy, never an Immich write.
    ),
    **_classified(
        ActionSafetyClass.DURABLE_OPERATION,
        "start_highlight_backfill",
        "create_backfill",
        "rebuild_global_order",
        "create_reanalysis",
        "decide_library_review_proposal",
        "control_reanalysis",
        "agent_retry_failed_reanalysis",
        "agent_ensure_reanalysis_complete",
        "agent_pause_reanalysis",
        "agent_stop_reanalysis",
        "agent_dismiss_reanalysis_failures",
        "agent_prune_history",
        "agent_resume_reanalysis",
        "retry_asset_from_immich",
        "restore_asset_to_star",
        "reanalyze_asset_check",
        "start_owner_rating_session",
        "advance_owner_rating_session",
        "cancel_owner_rating_session",
    ),
    **_classified(
        ActionSafetyClass.LOCAL_DESTRUCTIVE,
        "remove_asset_from_star",
        "execute_library_reset",
    ),
    **_classified(
        ActionSafetyClass.WORKER_PROTOCOL,
        "control_worker",
        "control_all_workers",
        "update_model_concurrency",
        "update_apple_queues",
        "update_worker_schedule",
        "worker_heartbeat",
        "discover_worker_models",
        "rotate_worker_credential",
        "drain_worker",
        "remove_offline_worker",
        "automatically_enroll_worker",
        "lease_task",
        "complete_model_warmup_task",
        "complete_media_stage_task",
        "heartbeat_task_lease",
        "store_task_normalized_media",
        "complete_semantic_embedding_task",
        "complete_highlight_comparison_task",
        "complete_series_selection_task",
        "complete_asset_vlm_task",
        "complete_library_review_task",
        "fail_task",
    ),
    **_classified(
        ActionSafetyClass.IMMICH_CONNECTION,
        "add_instance",
        "add_connection",
        "rotate_credential",
        "remove_connection",
    ),
    **_classified(
        ActionSafetyClass.IMMICH_WRITEBACK,
        "prepare_writeback",
        "approve_writeback",
        "cancel_writeback",
    ),
}

# HTTP GET is normally read-only. These compatibility endpoints are the small,
# explicit exception: they renew a session, seed a missing immutable/default
# catalog row, update cache/package access metadata, or advance an authenticated
# worker artifact hand-off. Keeping the list beside the action contract makes
# the exception reviewable and lets the request dependency put the same reset
# barrier around it as POST/PUT/PATCH/DELETE.
STATE_CHANGING_GET_ENDPOINTS: frozenset[str] = frozenset(
    {
        "current_session",
        "get_asset_preview",
        "get_asset_thumbnail",
        "get_group_task_preview",
        "get_library_program",
        "get_library_program_schedule",
        "get_policy",
        "get_task_preview",
        "get_worker_prompt",
        "inspect_asset",
        "list_review_prompts",
    }
)

# These GET handlers call commit only to close an authentication/read
# transaction before local network or model I/O. They are intentionally absent
# from the mutating contract; the source-based test keeps this exception finite.
READ_ONLY_GET_COMMIT_ENDPOINTS: frozenset[str] = frozenset(
    {
        "get_person_thumbnail",
        "list_gallery_assets",
        # Seeds the encrypted default roster on first read; no other state.
        "list_family_roster",
    }
)

ACTION_SAFETY_BY_ENDPOINT.update(_classified(ActionSafetyClass.AUTHENTICATION, "current_session"))
ACTION_SAFETY_BY_ENDPOINT.update(
    _classified(
        ActionSafetyClass.OWNER_CONFIGURATION,
        "create_family_roster_member",
        "delete_family_roster_member",
        "get_library_program",
        "get_library_program_schedule",
        "get_policy",
        "list_review_prompts",
        "update_family_roster_member",
    )
)
ACTION_SAFETY_BY_ENDPOINT.update(
    _classified(
        ActionSafetyClass.WORKER_PROTOCOL,
        "get_group_task_preview",
        "get_worker_prompt",
        "get_task_preview",
    )
)
ACTION_SAFETY_BY_ENDPOINT.update(
    _classified(
        ActionSafetyClass.IMMICH_CONNECTION,
        "get_asset_preview",
        "get_asset_thumbnail",
        "inspect_asset",
    )
)


def validate_action_safety_contract(endpoint_names: set[str]) -> None:
    declared = set(ACTION_SAFETY_BY_ENDPOINT)
    missing = sorted(endpoint_names - declared)
    stale = sorted(declared - endpoint_names)
    if missing or stale:
        raise RuntimeError(
            "mutating API action safety contract drift: "
            f"missing={missing or 'none'} stale={stale or 'none'}"
        )


__all__ = [
    "ACTION_SAFETY_BY_ENDPOINT",
    "ACTION_SAFETY_PROFILES",
    "READ_ONLY_GET_COMMIT_ENDPOINTS",
    "STATE_CHANGING_GET_ENDPOINTS",
    "ActionSafetyClass",
    "ActionSafetyProfile",
    "validate_action_safety_contract",
]
