from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.analysis.absolute_rating import ABSOLUTE_RATING_VERSION
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisPublicationSnapshot,
    AnalysisRun,
    Asset,
    AssetRevision,
    PersonalRankerVersion,
    Proposal,
    ReviewPromptRevision,
    Task,
    VisualFingerprint,
)
from star.domain.enums import ProposalState, ProposalType, TaskState
from star.services.audit import AuditRecord, append_audit_event
from star.services.campaigns import canonical_digest
from star.services.inference_identity import (
    attach_preview_digests,
    build_generation_inference_profiles,
)

PIPELINE_STAGES = (
    "source",
    "embedding",
    "primary",
    "similarity",
    "absolute_rating",
    "publication",
    "qwen",
    "highlights",
)

PRODUCTION_REPEATABILITY_TASKS = {
    "ministral-3:8b-instruct-2512-q4_K_M": frozenset({"primary_review"}),
    "qwen3-vl:8b-instruct-q4_K_M": frozenset({"group_compare"}),
}
STAGE_DESCENDANTS: dict[str, tuple[str, ...]] = {
    "source": PIPELINE_STAGES,
    "embedding": (
        "embedding",
        "similarity",
        "absolute_rating",
        "publication",
        "qwen",
        "highlights",
    ),
    "primary": (
        "primary",
        "similarity",
        "absolute_rating",
        "publication",
        "qwen",
        "highlights",
    ),
    "similarity": ("similarity", "highlights"),
    "absolute_rating": ("absolute_rating", "publication"),
    "publication": ("publication",),
    "qwen": ("qwen",),
    "highlights": ("highlights",),
}
# These stages enrich the durable photo record, but the authoritative model
# path must publish without waiting for a CPU sidecar or an intermittently
# available apple. Primary review and absolute rating remain required.
OPTIONAL_STAGES = frozenset({"embedding", "similarity"})
# Start stages that older callers and stored runs still name.
LEGACY_START_STAGES = {"technical": "source", "apple_vision": "source"}
DYNAMIC_STAGES = frozenset({"qwen", "highlights"})
SINGLE_TASK_STAGES = frozenset({"primary", "qwen"})


def expanded_stages(*, mode: str, start_stage: str) -> tuple[str, ...]:
    start_stage = LEGACY_START_STAGES.get(start_stage, start_stage)
    if start_stage not in STAGE_DESCENDANTS:
        raise ValueError("unsupported analysis start stage")
    if mode == "step_only":
        return (start_stage,)
    if mode in {"complete_review", "refresh_source"}:
        return PIPELINE_STAGES
    if mode == "from_stage":
        return STAGE_DESCENDANTS[start_stage]
    raise ValueError("unsupported reanalysis mode")


async def current_analysis_signature(session: AsyncSession) -> dict[str, str]:
    """Return the review contract a photograph would be judged by today.

    A photograph carries the prompt, model policy and calibration it was judged
    with, so comparing those against these values says whether its rating still
    reflects what Star currently believes. This is what lets an improved prompt
    reach the photographs that were rated before it existed, instead of only
    the ones reviewed afterwards.
    """

    active_prompt_rows = list(
        (
            await session.execute(
                select(ReviewPromptRevision.prompt_key, ReviewPromptRevision.version).where(
                    ReviewPromptRevision.status == "active"
                )
            )
        ).all()
    )
    revisions = {str(prompt_key): int(version) for prompt_key, version in active_prompt_rows}
    return {
        "prompt_version": prompt_version_signature(revisions),
        "model_policy_version": "lean-review-v4",
        "calibration_version": ABSOLUTE_RATING_VERSION,
        "application_release": f"v{__version__.split('.', 1)[0]}",
    }


def prompt_version_signature(revisions: dict[str, int]) -> str:
    """Return the compact generation prompt signature for a revision snapshot.

    Historical generations carried only the review and rating revisions. The owner
    taste revision is appended only when one exists so older signatures keep matching
    their retained snapshots.
    """

    signature = (
        f"primary-r{revisions.get('primary_review', 0)};rating-r{revisions.get('rating_scale', 0)}"
    )
    taste_revision = revisions.get("owner_taste", 0)
    if taste_revision > 0:
        signature += f";taste-r{taste_revision}"
    return signature


async def create_analysis_generation(
    session: AsyncSession,
    *,
    asset: Asset,
    actor_id: uuid.UUID | None,
    reason: str,
    mode: str,
    start_stage: str,
    reuse_policy: str,
    publication_policy: str,
    scope: dict[str, object],
    prompt_revisions_override: dict[str, int] | None = None,
    supersede_active: bool = True,
) -> AnalysisGeneration:
    if asset.current_revision_id is None:
        raise ValueError("asset has no current revision")
    # Serialize sequence allocation and active-generation supersession per
    # asset. Automatic admission, a manual photo action, and a bulk re-review
    # may otherwise all observe the same MAX(sequence) and race the immutable
    # unique identity at commit time.
    locked_asset = await session.scalar(select(Asset).where(Asset.id == asset.id).with_for_update())
    if locked_asset is None or locked_asset.current_revision_id != asset.current_revision_id:
        raise ValueError("asset changed before analysis generation creation")
    if locked_asset.access_state != "available":
        raise ValueError("asset is not available for analysis")
    asset = locked_asset
    stages = expanded_stages(mode=mode, start_stage=start_stage)
    sequence = (
        int(
            await session.scalar(
                select(func.max(AnalysisGeneration.sequence)).where(
                    AnalysisGeneration.asset_id == asset.id,
                    AnalysisGeneration.asset_revision_id == asset.current_revision_id,
                )
            )
            or 0
        )
        + 1
    )
    active_ranker = await session.scalar(
        select(PersonalRankerVersion)
        .where(PersonalRankerVersion.status == "active")
        .order_by(PersonalRankerVersion.version.desc())
        .limit(1)
    )
    if prompt_revisions_override is None:
        active_prompt_rows = list(
            (
                await session.execute(
                    select(ReviewPromptRevision.prompt_key, ReviewPromptRevision.version).where(
                        ReviewPromptRevision.status == "active"
                    )
                )
            ).all()
        )
        active_prompt_revisions = {
            str(prompt_key): int(version) for prompt_key, version in active_prompt_rows
        }
    else:
        active_prompt_revisions = dict(prompt_revisions_override)
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        parent_generation_id=asset.current_generation_id,
        sequence=sequence,
        scope={**scope, "execution_epoch": "verified-preferences-v1"},
        start_stage=start_stage,
        expanded_stages=list(stages),
        reuse_policy=reuse_policy,
        publication_policy=publication_policy,
        application_release=f"v{__version__.split('.', 1)[0]}",
        personal_ranker_version_id=active_ranker.id if active_ranker is not None else None,
        personal_ranker_version=active_ranker.version if active_ranker is not None else None,
        # This field tracks the per-photo review contract. The separately
        # versioned queue/pipeline contract is progressive-review-v11.
        model_policy_version="lean-review-v4",
        inference_profiles=await build_generation_inference_profiles(session),
        prompt_version=prompt_version_signature(active_prompt_revisions),
        prompt_revisions=active_prompt_revisions,
        schema_version="photo-observations-v1",
        calibration_version=ABSOLUTE_RATING_VERSION,
        state="building",
        reason=reason,
        created_by=actor_id,
    )
    session.add(generation)
    await session.flush()
    superseded_generations = (
        list(
            await session.scalars(
                select(AnalysisGeneration).where(
                    AnalysisGeneration.asset_id == asset.id,
                    AnalysisGeneration.id != generation.id,
                    AnalysisGeneration.state == "building",
                )
            )
        )
        if supersede_active
        else []
    )
    if superseded_generations:
        for replaced in superseded_generations:
            # Named so a wave that keeps replacing its own work is visible.
            replaced.failure_class = f"replaced_by_{reason}"
        superseded_ids = [value.id for value in superseded_generations]
        obsolete_tasks = list(
            await session.scalars(
                select(Task).where(
                    Task.analysis_generation_id.in_(superseded_ids),
                    Task.state.in_(
                        (
                            TaskState.PLANNED,
                            TaskState.QUEUED,
                            TaskState.RUNNING,
                            TaskState.RETRY_WAITING,
                            TaskState.CANCEL_REQUESTED,
                        )
                    ),
                )
            )
        )
        for task in obsolete_tasks:
            was_running = task.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            task.state = TaskState.CANCEL_REQUESTED if was_running else TaskState.CANCELLED
            task.cancel_requested_at = task.cancel_requested_at or utc_now()
            task.last_error_class = "AnalysisGenerationSuperseded"
        for prior in superseded_generations:
            prior.state = "superseded"
            prior.completed_at = utc_now()
            prior.superseded_at = prior.completed_at
    for stage in stages:
        # AssetRevision is the immutable source snapshot. Bytes are relayed only
        # when downstream work leases, so no separate persistent source task exists.
        state = (
            "succeeded"
            if stage == "source"
            else "not_applicable"
            if stage in DYNAMIC_STAGES
            else "planned"
        )
        applicable = stage not in DYNAMIC_STAGES
        node = AnalysisNode(
            generation_id=generation.id,
            asset_id=asset.id,
            stage_key=stage,
            state=state,
            required=stage not in OPTIONAL_STAGES and applicable,
            applicable=applicable,
            analyzer_version="v92",
            model_key=None,
            inference_profile=(
                "reasoning-v2"
                if stage == "qwen"
                else "comparison-v2"
                if stage == "highlights"
                else "structured-v2"
            ),
            input_digest=canonical_digest(
                {
                    "asset_id": str(asset.id),
                    "asset_revision_id": str(asset.current_revision_id),
                    "generation_id": str(generation.id),
                    "stage": stage,
                    "reuse_policy": reuse_policy,
                }
            ),
            output_digest=(
                canonical_digest({"stage": stage, "decision": "awaiting_policy"})
                if stage in DYNAMIC_STAGES
                else None
            ),
            decision=(
                {"reason_code": "awaiting_policy", "applicable": False}
                if stage in DYNAMIC_STAGES
                else {}
            ),
            completed_at=utc_now() if state == "succeeded" else None,
        )
        session.add(node)
    await session.flush()
    return generation


async def ensure_automatic_analysis_generation(
    session: AsyncSession,
    *,
    asset: Asset,
    task: Task,
    stage: str,
) -> AnalysisGeneration:
    if task.analysis_generation_id is not None:
        generation = await session.get(AnalysisGeneration, task.analysis_generation_id)
        if generation is None:
            raise RuntimeError("analysis generation is unavailable")
        await bind_generation_task(session, generation_id=generation.id, stage=stage, task=task)
        return generation
    locked_asset = await session.scalar(select(Asset).where(Asset.id == asset.id).with_for_update())
    if locked_asset is None or locked_asset.current_revision_id != asset.current_revision_id:
        raise RuntimeError("asset changed before analysis generation creation")
    manual_run_id = task.target_ref.get("manual_run_id")
    if manual_run_id is not None:
        generation = await create_analysis_generation(
            session,
            asset=locked_asset,
            actor_id=None,
            reason=str(task.target_ref.get("user_request_reason") or "manual_reanalysis"),
            mode="complete_review",
            start_stage="source",
            reuse_policy="none",
            publication_policy="atomic",
            scope={"kind": "manual", "manual_run_id": str(manual_run_id)},
        )
    else:
        generation = await session.scalar(
            select(AnalysisGeneration)
            .where(
                AnalysisGeneration.asset_id == locked_asset.id,
                AnalysisGeneration.asset_revision_id == locked_asset.current_revision_id,
                AnalysisGeneration.state == "building",
                AnalysisGeneration.reason == "automatic_review",
            )
            .order_by(AnalysisGeneration.sequence.desc())
            .limit(1)
        )
        if generation is None:
            generation = await create_analysis_generation(
                session,
                asset=locked_asset,
                actor_id=None,
                reason="automatic_review",
                mode="complete_review",
                start_stage="source",
                reuse_policy="compatible",
                publication_policy="atomic",
                scope={"kind": "automatic"},
            )
    await bind_generation_task(session, generation_id=generation.id, stage=stage, task=task)
    return generation


async def bind_generation_task(
    session: AsyncSession,
    *,
    generation_id: uuid.UUID | None,
    stage: str,
    task: Task,
    required: bool | None = None,
) -> None:
    if generation_id is None:
        return
    node = await session.scalar(
        select(AnalysisNode)
        .where(
            AnalysisNode.generation_id == generation_id,
            AnalysisNode.stage_key == stage,
        )
        .with_for_update()
    )
    if node is None:
        return
    task.analysis_generation_id = generation_id
    task.target_ref = {**task.target_ref, "analysis_generation_id": str(generation_id)}
    if node.state == "succeeded" and node.task_id != task.id:
        # A completed stage is immutable within one generation.  A later
        # planner must never replace its provenance with a duplicate task.
        return
    if stage in SINGLE_TASK_STAGES and node.task_id not in {None, task.id}:
        # One immutable generation has one canonical implementation task for a
        # required model stage. Silently rebinding here lets a repair planner
        # steal ownership from an in-flight worker and makes the first valid
        # result look like a stale payload.
        raise RuntimeError(f"analysis generation {stage} stage already has a canonical task")
    node.task_id = task.id
    node.state = "ready"
    if stage == "primary" or task.target_ref.get("reanalysis_run_id"):
        from star.services.run_summary import bind_run_generation

        await bind_run_generation(session, task=task)
    # ``create_analysis_generation`` is the authority for whether a stage votes
    # on publication.  Attaching an implementation task must not silently turn
    # optional Apple Vision or embedding work into a required predecessor.
    if required is not None:
        node.required = required
    node.applicable = True
    node.decision = {"reason_code": "task_materialized", "applicable": True}
    node.output_digest = None
    node.model_key = str(task.target_ref.get("model_key") or "") or None
    node.analyzer_version = str(
        task.target_ref.get("processor") or task.target_ref.get("processor_version") or "v92"
    )


async def settle_generation_task_node(
    session: AsyncSession,
    *,
    task: Task,
    stage: str,
) -> bool:
    """Settle a generation node in the same transaction as its task result.

    Background reconciliation remains useful for crash recovery, but normal
    completion must not expose a successful task behind a still-ready node.
    That gap previously allowed primary backfill to enqueue a second model run.
    """

    if task.analysis_generation_id is None:
        return False
    node = await session.scalar(
        select(AnalysisNode)
        .where(
            AnalysisNode.generation_id == task.analysis_generation_id,
            AnalysisNode.stage_key == stage,
        )
        .with_for_update()
    )
    if node is None:
        raise RuntimeError(f"analysis generation has no {stage} node")
    if node.task_id not in {None, task.id}:
        if node.state == "succeeded":
            return False
        raise RuntimeError(f"analysis generation {stage} node belongs to another task")

    node.task_id = task.id
    node.state = "succeeded"
    node.applicable = True
    node.attempt_count = task.attempt_count
    node.failure_class = None
    node.worker_runtime_ms = int(task.progress.get("duration_ms") or 0)
    raw_run_id = task.progress.get("analysis_run_id")
    run: AnalysisRun | None = None
    if raw_run_id:
        try:
            node.result_analysis_run_id = uuid.UUID(str(raw_run_id))
        except ValueError:
            node.result_analysis_run_id = None
    if node.result_analysis_run_id is not None:
        run = await session.get(AnalysisRun, node.result_analysis_run_id)
    node.output_digest = (
        run.result_digest
        if run is not None and run.result_digest is not None
        else canonical_digest(
            {
                "task_id": str(task.id),
                "task_type": task.task_type,
                "state": str(task.state),
                "progress": task.progress,
            }
        )
    )
    node.decision = {"reason_code": "task_succeeded", "applicable": True}
    node.completed_at = utc_now()
    return True


async def set_stage_applicability(
    session: AsyncSession,
    *,
    generation_id: uuid.UUID | None,
    stage: str,
    applicable: bool,
    reason_code: str,
    required: bool | None = None,
) -> None:
    """Persist a routing decision before publication can inspect the node."""

    if generation_id is None:
        return
    node = await session.scalar(
        select(AnalysisNode).where(
            AnalysisNode.generation_id == generation_id,
            AnalysisNode.stage_key == stage,
        )
    )
    if node is None:
        raise RuntimeError(f"analysis generation has no {stage} node")
    decision = {"reason_code": reason_code, "applicable": applicable}
    node.applicable = applicable
    node.required = applicable if required is None else applicable and required
    node.decision = decision
    node.output_digest = canonical_digest({"stage": stage, **decision})
    if applicable:
        if node.state == "not_applicable":
            node.state = "planned"
            node.completed_at = None
    else:
        if node.task_id is not None:
            task = await session.get(Task, node.task_id)
            if task is not None and task.state in {
                TaskState.PLANNED,
                TaskState.QUEUED,
                TaskState.RETRY_WAITING,
            }:
                task.state = TaskState.CANCELLED
                task.last_error_class = "StageNotApplicable"
        node.state = "not_applicable"
        node.completed_at = utc_now()


async def reconcile_generation_nodes(session: AsyncSession, *, limit: int = 500) -> int:
    task_node_states = {
        TaskState.RUNNING: "leased",
        TaskState.RETRY_WAITING: "retry_wait",
        TaskState.SUCCEEDED: "succeeded",
        TaskState.FAILED: "failed",
        TaskState.CANCELLED: "cancelled",
        TaskState.CANCEL_REQUESTED: "superseded",
    }
    candidates = list(
        (
            await session.execute(
                select(AnalysisNode.id, AnalysisNode.generation_id)
                .join(Task, Task.id == AnalysisNode.task_id)
                .where(
                    AnalysisNode.state.in_({"ready", "leased", "retry_wait"}),
                    AnalysisNode.state
                    != case(
                        *[
                            (Task.state == state, value)
                            for state, value in task_node_states.items()
                        ],
                        else_=AnalysisNode.state,
                    ),
                    Task.state.in_(
                        {
                            TaskState.RUNNING,
                            TaskState.RETRY_WAITING,
                            TaskState.SUCCEEDED,
                            TaskState.FAILED,
                            TaskState.CANCELLED,
                            TaskState.CANCEL_REQUESTED,
                        }
                    ),
                )
                .limit(limit)
            )
        ).all()
    )
    changed = 0
    for node_id, generation_id in candidates:
        # Completion takes a generation key-share lock before task and node.
        # Reconciliation must take the generation update lock before touching
        # the node, otherwise the two paths form generation->node / node->
        # generation deadlock cycles and stale reconciliation can overwrite a
        # just-committed successful node.
        locked_generation = await session.scalar(
            select(AnalysisGeneration)
            .where(AnalysisGeneration.id == generation_id)
            .with_for_update(skip_locked=True)
        )
        if locked_generation is None:
            continue
        row = (
            await session.execute(
                select(AnalysisNode, Task)
                .join(Task, Task.id == AnalysisNode.task_id)
                .where(AnalysisNode.id == node_id)
            )
        ).one_or_none()
        if row is None:
            continue
        node, task = row
        new_state = task_node_states[task.state]
        if node.state == new_state:
            continue
        node.state = new_state
        node.attempt_count = task.attempt_count
        node.failure_class = task.last_error_class
        node.worker_runtime_ms = int(task.progress.get("duration_ms") or 0)
        raw_run_id = task.progress.get("analysis_run_id")
        if raw_run_id:
            try:
                node.result_analysis_run_id = uuid.UUID(str(raw_run_id))
            except ValueError:
                node.result_analysis_run_id = None
        if new_state == "succeeded":
            run = (
                await session.get(AnalysisRun, node.result_analysis_run_id)
                if node.result_analysis_run_id is not None
                else None
            )
            node.output_digest = (
                run.result_digest
                if run is not None and run.result_digest is not None
                else canonical_digest(
                    {
                        "task_id": str(task.id),
                        "task_type": task.task_type,
                        "state": str(task.state),
                        "progress": task.progress,
                    }
                )
            )
            node.decision = {"reason_code": "task_succeeded", "applicable": True}
        if new_state == "leased" and node.started_at is None:
            node.started_at = utc_now()
        if new_state in {"succeeded", "failed", "cancelled", "superseded"}:
            node.completed_at = utc_now()
        changed += 1
    return changed


def _finish_evidence_node(
    node: AnalysisNode,
    *,
    state: str = "succeeded",
    evidence: dict[str, object],
) -> bool:
    """Complete a policy stage only with a reproducible persisted result digest."""
    digest = canonical_digest(evidence)
    if node.state == state and node.output_digest == digest:
        return False
    node.state = state
    node.output_digest = digest
    node.completed_at = utc_now()
    return True


async def rematerialize_final_rating(
    session: AsyncSession,
    generation_id: uuid.UUID,
) -> Proposal | None:
    """Refresh the final individual proposal after a later authority lands.

    The senior review completes after publication, so the generation's final
    proposal is recomputed in place; publication state is unchanged.
    """

    generation = await session.get(AnalysisGeneration, generation_id)
    if generation is None:
        return None
    return await _materialize_final_rating(session, generation)


async def _materialize_final_rating(
    session: AsyncSession,
    generation: AnalysisGeneration,
) -> Proposal | None:
    proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.analysis_generation_id == generation.id,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
                Proposal.authority_stage.in_({"primary", "qwen", "recheck", "highlights", "final"}),
            )
            .order_by(Proposal.created_at, Proposal.id)
        )
    )
    existing = next(
        (proposal for proposal in proposals if proposal.authority_stage == "final"),
        None,
    )
    sources = [proposal for proposal in proposals if proposal.authority_stage != "final"]
    # The NVIDIA worker primary is the individual authority until the Apple worker
    # Advanced (senior) review lands; the senior star then replaces it as is,
    # including a five: the Advanced review is the automated five-star
    # authority and nothing downstream caps it again. An owner-applied Recheck
    # answer (services.benchmark.apply_run) outranks both for this generation;
    # a fresh re-review opens a new generation and starts over. The Highlight
    # League only orders the tiers.
    precedence = {"primary": 1, "qwen": 2, "recheck": 3}
    selected = max(
        sources,
        key=lambda proposal: (
            precedence.get(proposal.authority_stage or "", 0),
            proposal.created_at,
            str(proposal.id),
        ),
        default=None,
    )
    if selected is None:
        return existing
    value = {
        **selected.value,
        "source": "final_materialized_proposal",
        "authority_source": selected.authority_stage,
        "upstream_proposal_ids": [str(proposal.id) for proposal in sources],
        "proposal_only": True,
        "does_not_authorize_write_or_deletion": True,
    }
    final = existing or Proposal(
        campaign_id=selected.campaign_id,
        analysis_generation_id=generation.id,
        proposal_type=ProposalType.RATING,
        target_type="asset",
        target_id=generation.asset_id,
        value=value,
        confidence=selected.confidence,
        reason_codes=["final_rating_materialized"],
        explanation=selected.explanation,
        state=ProposalState.PENDING,
        analysis_version="lean-review-v4:individual-final",
        authority_stage="final",
    )
    final.value = value
    final.confidence = selected.confidence
    final.reason_codes = [
        "final_rating_materialized",
        f"authority_{selected.authority_stage}",
    ]
    final.explanation = selected.explanation
    if existing is None:
        session.add(final)
        await session.flush()
    return final


async def revision_still_describes_the_same_photograph(
    session: AsyncSession,
    *,
    generation: AnalysisGeneration,
    asset: Asset,
) -> bool:
    """Whether a changed revision is the same picture with different metadata.

    A review is about the photograph. When Immich reports a new rating, a
    favourite, an archive flag or fresh EXIF, Star records a new revision, and
    treating that as a different photograph threw away a completed review and
    every task behind it. The pixels decide: same file size and same dimensions
    means the review still describes what it looked at.

    When the dimensions or size are unknown on either side there is nothing to
    compare, so the older, stricter answer stands and the generation is
    superseded rather than published against an unverified image.
    """

    if asset.current_revision_id is None:
        return False
    current = await session.get(AssetRevision, asset.current_revision_id)
    prior = await session.get(AssetRevision, generation.asset_revision_id)
    if current is None or prior is None:
        return False
    # A photograph on its way out of the library is not one to publish.
    if current.is_trashed:
        return False
    # Same dimensions/length alone cannot establish equivalence when Immich
    # provides explicit evidence that the image bytes or edit state changed.
    for key in ("checksum", "isEdited", "livePhotoVideoId"):
        before, after = prior.normalized_metadata.get(key), current.normalized_metadata.get(key)
        if before != after and (before is not None or after is not None):
            return False
    identity = (current.file_size, current.width, current.height)
    if any(value is None for value in identity):
        return False
    return identity == (prior.file_size, prior.width, prior.height) and (
        current.duration_seconds == prior.duration_seconds
    )


async def advance_analysis_generations(
    session: AsyncSession,
    *,
    limit: int = 500,
    completed_since: datetime | None = None,
) -> int:
    """Advance virtual DAG nodes and atomically publish complete generations."""

    if limit <= 0:
        return 0
    active_states = {"building", "complete_unpublished"}
    generation_ids: list[uuid.UUID] = []
    # Use the stage/state index to pull ready work rather than sorting every
    # building generation with correlated subqueries. This remains bounded when
    # the durable library grows to hundreds of thousands of photos.
    for stage_key in ("publication", "primary"):
        remaining = limit - len(generation_ids)
        if remaining <= 0:
            break
        # A succeeded publication node stays succeeded after its generation
        # publishes, so a hundred thousand of them sit under published
        # generations and this read walked every one, oldest first, each
        # cycle, usually to find nothing. Work that is ready to publish
        # completed recently: the per-cycle read stays inside the window the
        # stage/state/completed_at index bounds, and a full sweep once an hour
        # still catches anything older.
        statement = (
            select(AnalysisNode.generation_id)
            .join(AnalysisGeneration, AnalysisGeneration.id == AnalysisNode.generation_id)
            .where(
                AnalysisGeneration.state.in_(active_states),
                AnalysisNode.stage_key == stage_key,
                AnalysisNode.state == "succeeded",
            )
            .order_by(AnalysisNode.completed_at, AnalysisNode.id)
            .limit(remaining)
        )
        if completed_since is not None:
            statement = statement.where(AnalysisNode.completed_at >= completed_since)
        if generation_ids:
            statement = statement.where(AnalysisNode.generation_id.not_in(generation_ids))
        generation_ids.extend(await session.scalars(statement))
    remaining = limit - len(generation_ids)
    if remaining > 0:
        failed_required = exists(
            select(AnalysisNode.id).where(
                AnalysisNode.generation_id == AnalysisGeneration.id,
                AnalysisNode.required.is_(True),
                AnalysisNode.applicable.is_(True),
                AnalysisNode.state.in_({"failed", "cancelled"}),
            )
        )
        fallback = (
            select(AnalysisGeneration.id)
            .outerjoin(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(
                AnalysisGeneration.state.in_(active_states),
                or_(
                    AnalysisGeneration.state == "complete_unpublished",
                    failed_required,
                    Asset.id.is_(None),
                    Asset.current_revision_id.is_(None),
                    Asset.access_state.in_({"quarantined", "excluded", "deleted"}),
                    Asset.current_revision_id != AnalysisGeneration.asset_revision_id,
                    # Keep explicit stage-only jobs on their original path;
                    # only ordinary primary jobs can be safely skipped while waiting.
                    ~exists(
                        select(AnalysisNode.id).where(
                            AnalysisNode.generation_id == AnalysisGeneration.id,
                            AnalysisNode.stage_key == "primary",
                        )
                    ),
                ),
            )
            .order_by(AnalysisGeneration.created_at, AnalysisGeneration.id)
            .limit(remaining)
        )
        if generation_ids:
            fallback = fallback.where(AnalysisGeneration.id.not_in(generation_ids))
        generation_ids.extend(await session.scalars(fallback))
    generation_by_id = {
        generation.id: generation
        for generation in await session.scalars(
            select(AnalysisGeneration)
            .where(AnalysisGeneration.id.in_(generation_ids))
            .with_for_update(skip_locked=True)
        )
    }
    generations = [generation_by_id[item] for item in generation_ids if item in generation_by_id]
    changed = 0
    for generation in generations:
        nodes = {
            node.stage_key: node
            for node in await session.scalars(
                select(AnalysisNode).where(AnalysisNode.generation_id == generation.id)
            )
        }
        asset = await session.get(Asset, generation.asset_id)
        if asset is not None and asset.access_state in {"quarantined", "excluded", "deleted"}:
            generation.state = "excluded"
            generation.failure_class = "SourceMediaQuarantined"
            generation.completed_at = utc_now()
            changed += 1
            continue
        if asset is None or (
            asset.current_revision_id != generation.asset_revision_id
            and not await revision_still_describes_the_same_photograph(
                session, generation=generation, asset=asset
            )
        ):
            # Why a review was thrown away is the only thing that explains a
            # wave whose machines are busy and whose photographs never land.
            generation.failure_class = (
                "asset_missing" if asset is None else "asset_revision_changed"
            )
            generation.state = "superseded"
            generation.superseded_at = utc_now()
            changed += 1
            continue
        embedding = nodes.get("embedding")
        source = nodes.get("source")
        source_evidence = next(
            (
                nodes[key]
                for key in ("primary", "embedding", "qwen", "highlights")
                if key in nodes and nodes[key].state == "succeeded" and nodes[key].input_digest
            ),
            None,
        )
        if source is not None and source.state == "planned" and source_evidence is not None:
            source.input_digest = source_evidence.input_digest
            changed += int(
                _finish_evidence_node(
                    source,
                    evidence={
                        "stage": "source",
                        "asset_revision_id": str(generation.asset_revision_id),
                        "preview_digest": source_evidence.input_digest,
                    },
                )
            )
        primary = nodes.get("primary")
        similarity = nodes.get("similarity")
        qwen = nodes.get("qwen")
        absolute = nodes.get("absolute_rating")
        highlights = nodes.get("highlights")
        publication = nodes.get("publication")
        if (
            similarity is not None
            and similarity.state == "planned"
            and embedding is not None
            and embedding.state == "succeeded"
            and primary is not None
            and primary.state == "succeeded"
        ):
            fingerprint = await session.scalar(
                select(VisualFingerprint.id).where(
                    VisualFingerprint.asset_id == generation.asset_id,
                    VisualFingerprint.asset_revision_id == generation.asset_revision_id,
                    VisualFingerprint.algorithm == "phash",
                    VisualFingerprint.algorithm_version == "imagehash-4.3.2-hash16",
                    VisualFingerprint.similarity_checked_at.is_not(None),
                )
            )
            if fingerprint is not None:
                changed += int(
                    _finish_evidence_node(
                        similarity,
                        evidence={
                            "stage": "similarity",
                            "state": "succeeded",
                            "fingerprint_id": str(fingerprint),
                        },
                    )
                )
        qwen_ready = primary is not None and primary.state == "succeeded"
        if qwen is not None and qwen.applicable and qwen.required:
            qwen_ready = qwen_ready and qwen.state == "succeeded"
        semantic_ready = qwen_ready
        if highlights is not None and highlights.applicable and highlights.required:
            semantic_ready = semantic_ready and highlights.state == "succeeded"
        if absolute is not None and absolute.state == "planned" and semantic_ready:
            run_exists = await session.scalar(
                select(AnalysisRun.id).where(
                    AnalysisRun.analysis_generation_id == generation.id,
                    AnalysisRun.analysis_type.in_({"primary_review", "photo_deep_review"}),
                    AnalysisRun.status == "succeeded",
                )
            )
            if run_exists is not None:
                final_proposal = await _materialize_final_rating(session, generation)
                changed += int(
                    _finish_evidence_node(
                        absolute,
                        evidence={
                            "stage": "absolute_rating",
                            "analysis_run_id": str(run_exists),
                            "final_proposal_id": (
                                str(final_proposal.id) if final_proposal is not None else None
                            ),
                            "qwen_required": bool(qwen and qwen.required),
                            "highlight_required": bool(highlights and highlights.required),
                        },
                    )
                )
        predecessors = [node for key, node in nodes.items() if key != "publication"]
        failed_predecessor = next(
            (
                node
                for node in predecessors
                if node.required and node.applicable and node.state in {"failed", "cancelled"}
            ),
            None,
        )
        if failed_predecessor is not None:
            generation.state = "failed"
            generation.failure_class = (
                failed_predecessor.failure_class
                or f"required_{failed_predecessor.stage_key}_{failed_predecessor.state}"
            )
            generation.completed_at = utc_now()
            changed += 1
            continue
        generation.inference_profiles = attach_preview_digests(
            generation.inference_profiles,
            nodes,
        )
        {node.model_key for node in predecessors if node.model_key}
        unstable_profile = None
        if publication is not None and publication.state == "planned" and unstable_profile:
            publication.state = "failed"
            publication.failure_class = "model_repeatability_unstable"
            publication.completed_at = utc_now()
            generation.state = "failed"
            generation.failure_class = "model_repeatability_unstable"
            generation.completed_at = utc_now()
            changed += 1
        if (
            publication is not None
            and publication.state == "planned"
            and all(
                (not node.required or not node.applicable or node.state == "succeeded")
                for node in predecessors
            )
        ):
            changed += int(
                _finish_evidence_node(
                    publication,
                    evidence={
                        "stage": "publication",
                        "generation_id": str(generation.id),
                        "predecessors": {
                            node.stage_key: {
                                "state": node.state,
                                "output_digest": node.output_digest,
                            }
                            for node in predecessors
                        },
                    },
                )
            )
        if (
            publication is not None
            and publication.state == "succeeded"
            and await publish_generation(session, generation.id)
        ):
            changed += 1
    return changed


async def publish_generation(
    session: AsyncSession,
    generation_id: uuid.UUID,
) -> bool:
    generation = await session.scalar(
        select(AnalysisGeneration).where(AnalysisGeneration.id == generation_id).with_for_update()
    )
    if generation is None or generation.state not in {"building", "complete_unpublished"}:
        return False
    if generation.publication_policy != "atomic":
        generation.state = "complete_unpublished"
        generation.completed_at = utc_now()
        return False
    nodes: Sequence[AnalysisNode] = (
        await session.scalars(
            select(AnalysisNode).where(AnalysisNode.generation_id == generation.id)
        )
    ).all()
    if any(node.required and node.applicable and node.state != "succeeded" for node in nodes):
        return False
    asset = await session.scalar(
        select(Asset).where(Asset.id == generation.asset_id).with_for_update()
    )
    if asset is not None and asset.access_state in {"quarantined", "excluded", "deleted"}:
        generation.state = "excluded"
        generation.failure_class = "SourceMediaQuarantined"
        generation.completed_at = utc_now()
        return False
    if asset is None or (
        asset.current_revision_id != generation.asset_revision_id
        and not await revision_still_describes_the_same_photograph(
            session, generation=generation, asset=asset
        )
    ):
        generation.failure_class = "asset_missing" if asset is None else "asset_revision_changed"
        generation.state = "superseded"
        generation.superseded_at = utc_now()
        return False
    newer = await session.scalar(
        select(AnalysisGeneration.id).where(
            AnalysisGeneration.asset_id == generation.asset_id,
            AnalysisGeneration.asset_revision_id == generation.asset_revision_id,
            AnalysisGeneration.sequence > generation.sequence,
            AnalysisGeneration.state.not_in({"abandoned", "superseded", "excluded"}),
        )
    )
    if newer is not None:
        generation.failure_class = "newer_generation_exists"
        generation.state = "superseded"
        generation.superseded_at = utc_now()
        return False
    previous_id = asset.current_generation_id
    asset.current_generation_id = generation.id
    generation.state = "published"
    generation.completed_at = utc_now()
    generation.published_at = generation.completed_at
    if previous_id is not None and previous_id != generation.id:
        previous = await session.get(AnalysisGeneration, previous_id)
        if previous is not None and previous.state == "published":
            previous.state = "superseded"
            previous.failure_class = "replaced_after_publication"
            previous.superseded_at = generation.published_at
    from star.analysis.ranking import refresh_asset_curation_snapshot

    await refresh_asset_curation_snapshot(session, asset.id)
    await session.flush()
    from star.db.models import (
        AssetCurationSnapshot,
        CollectionGroup,
        CollectionGroupMember,
        HighlightLeagueEntry,
    )

    curation = await session.get(AssetCurationSnapshot, asset.id)
    highlight = await session.scalar(
        select(HighlightLeagueEntry)
        .where(HighlightLeagueEntry.asset_id == asset.id)
        .order_by(HighlightLeagueEntry.updated_at.desc())
        .limit(1)
    )
    membership = await session.scalar(
        select(CollectionGroupMember)
        .where(CollectionGroupMember.asset_id == asset.id)
        .order_by(CollectionGroupMember.group_id)
        .limit(1)
    )
    series = await session.get(CollectionGroup, membership.group_id) if membership else None
    rating_proposal = await session.scalar(
        select(Proposal)
        .where(
            Proposal.analysis_generation_id == generation.id,
            Proposal.proposal_type == "rating",
        )
        .order_by(Proposal.created_at.desc())
        .limit(1)
    )
    snapshot_payload = {
        "asset_id": str(asset.id),
        "asset_revision_id": str(generation.asset_revision_id),
        "analysis_generation_id": str(generation.id),
        "application_release": generation.application_release,
        "personal_ranker_version_id": (
            str(generation.personal_ranker_version_id)
            if generation.personal_ranker_version_id is not None
            else None
        ),
        "personal_ranker_version": generation.personal_ranker_version,
        "calibration_version": generation.calibration_version,
        "model_policy_version": generation.model_policy_version,
        "prompt_version": generation.prompt_version,
        "prompt_revisions": generation.prompt_revisions,
        "schema_version": generation.schema_version,
        "inference_profiles": generation.inference_profiles,
        "curation": (
            {
                "effective_rating": curation.effective_rating,
                "rating_source": curation.rating_source,
                "rank_score": curation.rank_score,
                "evidence_digest": curation.evidence_digest,
                "destinations": curation.destination_recommendations,
            }
            if curation is not None
            else None
        ),
        "series": (
            {
                "group_id": str(series.id),
                "group_type": str(series.group_type),
                "representative_asset_id": (
                    str(series.representative_asset_id)
                    if series.representative_asset_id is not None
                    else None
                ),
                "confidence": series.confidence,
                "source_facts": series.source_facts,
            }
            if series is not None
            else None
        ),
        "highlight": (
            {
                "generation": highlight.generation,
                "global_percentile": highlight.global_percentile,
                "mu": highlight.mu,
                "sigma": highlight.sigma,
                "five_star_state": highlight.five_star_state,
                "five_star_rank": highlight.five_star_rank,
            }
            if highlight is not None
            else None
        ),
        "rating_policy": (
            {
                "rating": rating_proposal.value.get("rating"),
                "semantic_score": rating_proposal.value.get("semantic_score"),
                "hard_caps": rating_proposal.value.get("hard_caps", []),
                "disqualifiers": rating_proposal.value.get("disqualifiers", []),
            }
            if rating_proposal is not None
            else None
        ),
        "privacy": (
            {
                "sensitive_content": rating_proposal.value.get("sensitive_content"),
                "privacy": rating_proposal.value.get("privacy"),
                "privacy_uncertain": rating_proposal.value.get("privacy_uncertain"),
            }
            if rating_proposal is not None
            else None
        ),
    }
    session.add(
        AnalysisPublicationSnapshot(
            asset_id=asset.id,
            asset_revision_id=generation.asset_revision_id,
            analysis_generation_id=generation.id,
            payload=snapshot_payload,
        )
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="analysis.generation.published",
            actor_type="system",
            actor_id="analysis-generation-coordinator",
            target_ref={"asset_id": str(asset.id), "generation_id": str(generation.id)},
            correlation_id=str(generation.id),
            before={"generation_id": str(previous_id) if previous_id else None},
            after={
                "generation_id": str(generation.id),
                "asset_revision_id": str(generation.asset_revision_id),
                "calibration_version": generation.calibration_version,
                "publication_policy": generation.publication_policy,
            },
            details={"atomic": True, "immich_unchanged": True},
        ),
    )
    return True
