"""Bounded upgrade of unstarted primary work without rewriting prior evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from star.analysis.asset_vlm_submission import SENIOR_REVIEW_RUNTIMES
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    Asset,
    LibraryProgram,
    ModelVersion,
    Task,
    WorkRunAsset,
)
from star.domain.enums import TaskState
from star.services.analysis_generations import bind_generation_task, create_analysis_generation
from star.services.campaigns import canonical_digest
from star.services.run_summary import current_run_id


def cancel_generation_less_deep_review(task: Task, *, now: datetime) -> None:
    """Settle an Advanced review whose generation no longer exists.

    Housekeeping prunes a superseded generation and the foreign key leaves
    the task without one; completing it can publish nothing, so leasing it
    only burns a lane (v261: six such tasks held every Apple lane for the
    length of a lease while the worker had already given up on them).
    """

    task.state = TaskState.CANCELLED
    task.cancel_requested_at = task.cancel_requested_at or now
    task.last_error_class = "GenerationGone"
    task.progress = {**dict(task.progress or {}), "stage": "cancelled", "reason": "generation_gone"}
    task.leased_to_worker_id = None
    task.lease_id = None
    task.lease_expires_at = None


async def cancel_generation_less_deep_reviews(session: AsyncSession, *, limit: int = 500) -> int:
    """Cancel every planned, queued, waiting or running Advanced review without a generation."""

    now = utc_now()
    tasks = list(
        await session.scalars(
            select(Task)
            .where(
                Task.task_type == "asset.deep_review",
                Task.analysis_generation_id.is_(None),
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.RUNNING,
                    }
                ),
            )
            .order_by(Task.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for task in tasks:
        cancel_generation_less_deep_review(task, now=now)
    return len(tasks)


async def repair_misregistered_senior_tasks(session: AsyncSession, *, limit: int = 200) -> int:
    """Rebind unaccepted Qwen 3.8 work to a native senior registration.

    Older planners selected the first enabled model row by key. When an Ollama
    registration predated the native registrations, the worker correctly ran
    the native runtime but completion rejected the runtime identity. The senior
    key has two native registrations (MLX on the Mac, the llama.cpp fork on
    the NVIDIA worker); a task bound to either is left alone, since completion
    records the evidence against whichever one answered. Only unleased work and
    failures carrying that exact rejection are repaired; accepted evidence and
    unrelated terminal failures remain immutable.
    """

    native = list(
        (
            await session.scalars(
                select(ModelVersion)
                .where(
                    ModelVersion.model_id == "qwen3.8:27b",
                    ModelVersion.runtime.in_(SENIOR_REVIEW_RUNTIMES),
                    ModelVersion.enabled.is_(True),
                )
                .order_by(ModelVersion.created_at.desc(), ModelVersion.id.desc())
            )
        ).all()
    )
    cancelled = await cancel_generation_less_deep_reviews(session, limit=limit)
    if not native:
        return cancelled
    desired = native[0]
    accepted_ids = {str(model.id) for model in native}
    candidates = list(
        await session.scalars(
            select(Task)
            .where(
                Task.task_type == "asset.deep_review",
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.FAILED,
                    }
                ),
                Task.target_ref["model_key"].as_string() == "qwen3.8:27b",
                Task.target_ref["model_version_id"].as_string().not_in(accepted_ids),
            )
            .order_by(Task.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    changed = cancelled
    # Advanced reviews the coordinator rejected for a runtime label mismatch
    # (v253 to v257: "mlx-vlm" against "mlx") are bound to an accepted row
    # already; they get one fresh attempt now that labels fold together.
    rejected = list(
        await session.scalars(
            select(Task)
            .where(
                Task.task_type == "asset.deep_review",
                Task.state == TaskState.FAILED,
                Task.last_error_class == "ControlPlanePayloadRejected",
                Task.target_ref["model_key"].as_string() == "qwen3.8:27b",
                Task.target_ref["model_version_id"].as_string().in_(accepted_ids),
                Task.progress["reason"]
                .as_string()
                .is_distinct_from("senior_runtime_label_repaired"),
            )
            .order_by(Task.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for task in rejected:
        if task.analysis_generation_id is None:
            continue
        task.state = TaskState.QUEUED
        task.attempt_count = 0
        task.available_at = utc_now()
        task.last_error_class = None
        task.progress = {"stage": "queued", "reason": "senior_runtime_label_repaired"}
        changed += 1
    for task in candidates:
        if (
            task.state == TaskState.FAILED
            and task.last_error_class != "ControlPlanePayloadRejected"
        ):
            continue
        task.target_ref = {
            **task.target_ref,
            "model_key": desired.model_id,
            "model_version_id": str(desired.id),
            "runtime_registration": desired.runtime,
        }
        task.idempotency_key = canonical_digest(
            {
                "repairs": str(task.id),
                "model_version_id": str(desired.id),
                "contract": "senior-native-registration-v2",
            }
        )
        if task.state == TaskState.FAILED:
            task.state = TaskState.QUEUED
            task.attempt_count = 0
            task.available_at = utc_now()
            task.last_error_class = None
            task.progress = {
                "stage": "queued",
                "reason": "senior_runtime_registration_repaired",
            }
        await bind_generation_task(
            session,
            generation_id=task.analysis_generation_id,
            stage="qwen",
            task=task,
            required=False,
        )
        changed += 1
    return changed


async def requires_epoch_upgrade(
    session: AsyncSession, task: Task, program: LibraryProgram
) -> bool:
    if (
        task.task_type != "asset.primary_review"
        or task.attempt_count
        or task.asset_id is None
        or task.analysis_generation_id is None
    ):
        return False
    asset = await session.get(Asset, task.asset_id)
    generation = await session.get(AnalysisGeneration, task.analysis_generation_id)
    if (
        asset is None
        or generation is None
        or asset.analysis_generation != program.library_generation
        or asset.current_generation_id != generation.id
        or generation.state != "building"
        or generation.scope.get("execution_epoch")
    ):
        return False
    return not bool(
        await session.scalar(
            select(
                exists().where(
                    Task.analysis_generation_id == generation.id,
                    Task.task_type.in_({"asset.primary_review", "asset.deep_review"}),
                    Task.attempt_count > 0,
                )
            )
        )
    )


async def upgrade_unstarted_primary(
    session: AsyncSession, program: LibraryProgram, *, limit: int = 100
) -> int:
    if (
        not (program.model_pipeline_policy or {})
        .get("calibration_run_pins", {})
        .get(str(program.library_generation))
    ):
        return 0
    attempted = aliased(Task)
    candidates = list(
        await session.scalars(
            select(Task)
            .join(AnalysisGeneration, AnalysisGeneration.id == Task.analysis_generation_id)
            .join(Asset, Asset.id == Task.asset_id)
            .where(
                Task.task_type == "asset.primary_review",
                Task.state.in_({TaskState.PLANNED, TaskState.QUEUED}),
                Task.attempt_count == 0,
                Asset.current_generation_id == AnalysisGeneration.id,
                Asset.analysis_generation == program.library_generation,
                AnalysisGeneration.state == "building",
                AnalysisGeneration.scope["execution_epoch"].as_string().is_(None),
                Task.target_ref["execution_manifest_digest"].as_string().is_(None),
                ~exists(
                    select(attempted.id).where(
                        attempted.analysis_generation_id == AnalysisGeneration.id,
                        attempted.task_type.in_({"asset.primary_review", "asset.deep_review"}),
                        attempted.attempt_count > 0,
                    )
                ),
            )
            .order_by(Task.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Task)
        )
    )
    changed = 0
    for old in candidates:
        asset = await session.get(Asset, old.asset_id)
        prior = await session.get(AnalysisGeneration, old.analysis_generation_id)
        if asset is None or prior is None:
            continue
        generation = await create_analysis_generation(
            session,
            asset=asset,
            actor_id=None,
            reason="verified_preferences_epoch",
            mode="complete_review",
            start_stage="source",
            reuse_policy="compatible",
            publication_policy=prior.publication_policy,
            scope={**prior.scope, "execution_epoch": "verified-preferences-v1"},
            prompt_revisions_override=dict(prior.prompt_revisions),
            supersede_active=False,
        )
        prior.state = "superseded"
        prior.failure_class = "replaced_unstarted_execution_epoch"
        prior.superseded_at = utc_now()
        asset.current_generation_id = generation.id
        # Retain compatible factual evidence by reference; never count its compute twice.
        old_nodes = list(
            await session.scalars(
                select(AnalysisNode).where(
                    AnalysisNode.generation_id == prior.id,
                    AnalysisNode.stage_key.in_({"source", "technical", "apple_vision"}),
                    AnalysisNode.state == "succeeded",
                )
            )
        )
        for old_node in old_nodes:
            node = await session.scalar(
                select(AnalysisNode).where(
                    AnalysisNode.generation_id == generation.id,
                    AnalysisNode.stage_key == old_node.stage_key,
                )
            )
            if node is not None:
                node.state = "succeeded"
                node.output_digest = old_node.output_digest
                node.result_analysis_run_id = old_node.result_analysis_run_id
                node.completed_at = old_node.completed_at
                node.decision = {**old_node.decision, "reused_node_id": str(old_node.id)}
        replacement = Task(
            campaign_id=old.campaign_id,
            processing_batch_id=old.processing_batch_id,
            connection_id=old.connection_id,
            asset_id=old.asset_id,
            asset_revision_id=old.asset_revision_id,
            analysis_generation_id=generation.id,
            task_type=old.task_type,
            routing_key=old.routing_key,
            data_class=old.data_class,
            completion_role=old.completion_role,
            estimated_work_class=old.estimated_work_class,
            requirements=dict(old.requirements),
            priority=old.priority,
            max_attempts=old.max_attempts,
            state=TaskState.QUEUED,
            target_ref={
                **old.target_ref,
                "analysis_generation_id": str(generation.id),
                "execution_epoch": "verified-preferences-v1",
            },
            idempotency_key=canonical_digest(
                {"replaces": str(old.id), "epoch": "verified-preferences-v1"}
            ),
        )
        old.state = TaskState.CANCELLED
        old.last_error_class = "ReplacedByVerifiedPreferencesEpoch"
        session.add(replacement)
        await session.flush()
        await bind_generation_task(
            session, generation_id=generation.id, stage="primary", task=replacement
        )
        member = await session.get(WorkRunAsset, (current_run_id(program), asset.id))
        if member is not None:
            member.generation_id = generation.id
        changed += 1
    return changed
