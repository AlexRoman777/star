"""Content-addressed instructions for production model tasks."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import (
    AnalysisGeneration,
    ExecutionManifest,
    LibraryProgram,
    Task,
    TasteCalibrationSnapshot,
    Worker,
)
from star.inference.policy import APPLE_SYSTEM_PROMPT_KEY, NVIDIA_SYSTEM_PROMPT_KEY
from star.services.campaigns import canonical_digest

MANIFEST_TASKS = frozenset(
    {
        "asset.primary_review",
        "asset.deep_review",
        "group.highlight_compare",
        "group.series_select",
    }
)


def instruction_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def attach_execution_manifest(
    session: AsyncSession, task: Task, worker: Worker | None = None
) -> None:
    """Pin once, never silently replace the instructions on a retry."""
    if task.task_type not in MANIFEST_TASKS or task.target_ref.get("execution_manifest_digest"):
        return
    from star.services.review_prompts import (
        ensure_prompt_revision_history,
        prompt_revision,
        prompt_text_at_revision,
    )

    await ensure_prompt_revision_history(session, actor_id=None)
    target = dict(task.target_ref)
    generation = (
        await session.get(AnalysisGeneration, task.analysis_generation_id)
        if task.analysis_generation_id
        else None
    )
    revisions = dict(target.get("prompt_revisions") or {})
    senior = task.task_type == "asset.deep_review"
    keys = ["owner_taste"]
    if task.task_type.startswith("asset."):
        keys.extend(
            [
                "rating_scale",
                APPLE_SYSTEM_PROMPT_KEY if senior else NVIDIA_SYSTEM_PROMPT_KEY,
                f"photo_deep_review:{target.get('prompt_version') or 'senior-review-v1'}"
                if senior
                else "primary_review",
            ]
        )
    program = await session.scalar(select(LibraryProgram).limit(1))
    if program is not None:
        from star.services.execution_epoch import requires_epoch_upgrade

        if await requires_epoch_upgrade(session, task, program):
            raise ValueError("unstarted review generation is being upgraded")
    policy = dict(program.model_pipeline_policy or {}) if program else {}
    run_key = str(program.library_generation) if program else "standalone"
    pins = dict(policy.get("calibration_run_pins") or {})
    snapshot_id = target.get("owner_calibration_snapshot_id", pins.get(run_key))
    if program is not None and run_key not in pins:
        raise ValueError("run preference snapshot is being prepared")
    snapshot = (
        await session.get(TasteCalibrationSnapshot, uuid.UUID(str(snapshot_id)))
        if snapshot_id
        else None
    )
    if task.attempt_count > 0 or (
        generation is not None and not generation.scope.get("execution_epoch")
    ):
        snapshot = None  # Preserve the legacy instruction identity of attempted work.
    components: dict[str, Any] = {}
    role = "senior" if senior else "primary" if task.task_type.startswith("asset.") else "group"
    for key in keys:
        version = revisions.get(key) or await prompt_revision(session, key)
        text = await prompt_text_at_revision(session, prompt_key=key, version=version)
        if text is None:
            raise ValueError("execution prompt revision is unavailable")
        if key == "owner_taste" and snapshot is not None:
            learned = str(snapshot.payload.get(role, {}).get("text") or "")
            if learned:
                text = f"{text.rstrip()}\n\nLearned from the owner's corrections:\n{learned}"
        components[key] = {
            "revision": version,
            "instruction": text,
            "digest": instruction_digest(text),
        }
        revisions[key] = version
    from star.services.library_program import effective_inference_settings

    payload = {
        "runtime_contract": _runtime_contract(task, worker),
        "assembly_version": "verified-prompts-v1",
        "task_type": task.task_type,
        "model_key": target.get("model_key"),
        "prompt_version": target.get("prompt_version"),
        "components": components,
        # Owner-editable sampling, reasoning, and limits per stage (v209 B6).
        # Part of the digest, so a settings change yields a new manifest for
        # work leased afterwards while attempted work keeps its identity.
        "inference_settings": effective_inference_settings(program).model_dump(mode="json"),
        "calibration": (
            {"id": str(snapshot.id), "version": snapshot.version, "digest": snapshot.content_digest}
            if snapshot
            else None
        ),
    }
    digest = canonical_digest(payload)
    if await session.get(ExecutionManifest, digest) is None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from star.db.base import utc_now

        statement = (
            pg_insert(ExecutionManifest)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(ExecutionManifest)
        )
        await session.execute(
            statement.values(
                digest=digest, payload=payload, created_at=utc_now()
            ).on_conflict_do_nothing(index_elements=["digest"])
        )
    task.target_ref = {
        **target,
        # The worker reads the envelope from the task itself, not from the
        # manifest, so a contract reduction only reaches a runtime if it is
        # written here. Clamping in the manifest alone left v215 asking the
        # 8 GiB NVIDIA GPU for the old window on every already-queued photo.
        **_clamped_task_envelope(task),
        "prompt_revisions": revisions,
        "execution_manifest_digest": digest,
        "owner_calibration": payload["calibration"],
    }


def _clamped_task_envelope(task: Task) -> dict[str, int]:
    """Lower a stored envelope to the release contract, never raise it.

    Machine independent on purpose: the result is written onto the task, so it
    must stay correct whichever worker leases it next. Worker-specific
    narrowing belongs in ``_runtime_contract``.
    """

    from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS

    contract = TASK_RUNTIME_CONTRACTS.get(task.task_type)
    if contract is None:
        return {}
    ceilings = contract.payload()
    clamped: dict[str, int] = {}
    for key in ("context_tokens", "max_completion_tokens", "max_media_edge", "compute_budget_ms"):
        value = task.target_ref.get(key)
        ceiling = ceilings.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and isinstance(ceiling, int)
            and not isinstance(ceiling, bool)
            and value > ceiling
        ):
            clamped[key] = ceiling
    return clamped


def _runtime_contract(task: Task, worker: Worker | None) -> dict[str, Any]:
    from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS
    from star.services.worker_contract import required_task_revision

    contract = TASK_RUNTIME_CONTRACTS.get(task.task_type)
    profile: dict[str, Any] = dict(contract.payload()) if contract else {}
    # A task carries the envelope it was queued with, which is what lets a
    # fallback retry ask for less. It must never ask for more than the release
    # currently allows: a queue built before an envelope was reduced would
    # otherwise keep demanding the old window until it drained, and on the
    # 8 GiB NVIDIA that means the model is offloaded to the CPU on every one
    # of those photos. The contract is the ceiling; the task chooses below it.
    for key in ("context_tokens", "max_completion_tokens", "max_media_edge", "compute_budget_ms"):
        value = task.target_ref.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        ceiling = profile.get(key)
        profile[key] = (
            min(value, ceiling)
            if isinstance(ceiling, int) and not isinstance(ceiling, bool)
            else value
        )
    if (
        task.task_type == "group.highlight_compare"
        and worker
        and not worker.capabilities.get("apple-silicon")
    ):
        profile.update(context_tokens=4096, contact_sheet_edge=1344)
    profile["executor_revision"] = required_task_revision(task.task_type)
    profile["reasoning_effort"] = "medium" if task.task_type == "asset.deep_review" else "off"
    return profile
