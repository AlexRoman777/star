from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from star.analysis.asset_vlm_submission import (
    SENIOR_REVIEW_RUNTIMES,
    deep_review_priority,
    deep_review_processor_version,
    eligible_asset_vlm_models,
    enqueue_primary_review,
)
from star.analysis.ranking import refresh_asset_curation_snapshot
from star.analysis.review_pipeline import (
    PRIVACY_UNCERTAIN_CONFIDENCE,
    REVIEW_CONTRACT_VERSION,
    in_qwen_audit_sample,
    privacy_uncertain,
)
from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisRun,
    Asset,
    AssetRevision,
    DeepReviewBackfill,
    LibraryPolicyVersion,
    LibraryProgram,
    Proposal,
    Task,
    User,
)
from star.domain.enums import LibraryProgramState, MediaType, Role, TaskState
from star.domain.schemas import (
    DeepReviewExceptionPolicy,
    DeepReviewPolicyUpdate,
)
from star.services.campaigns import canonical_digest
from star.services.library_program import requested_run_count
from star.services.operation_locks import lock_operation_family

BACKFILL_TOKEN_TTL = timedelta(minutes=10)
PROMPT_EXPERIMENT_TOKEN_TTL = timedelta(minutes=10)
BACKFILL_CHUNK_SIZE = 500
BACKFILL_HOT_LIMIT = 500
PROMPT_EXPERIMENT_HOT_LIMIT = 500
PROMPT_EXPERIMENT_MATERIALIZE_BATCH = 100
PROMPT_EXPERIMENT_MINIMUM_OWNER_LABELS = 10
PROMPT_EXPERIMENT_SNAPSHOT_KEY = "_experiment_snapshot"
FROZEN_AUTOMATED_EVIDENCE_KEY = "frozen_automated_evidence"
FROZEN_EVIDENCE_CONTRACT_VERSION = "deep-curation-frozen-evidence-v1"
DEEP_EXPERIMENT_COMPUTE_BUDGET_MS = 600_000
_FROZEN_BUNDLE_LIMIT_BYTES = 28 * 1024
_PRIMARY_EVIDENCE_LIMIT_BYTES = 8 * 1024
_QUICK_EVIDENCE_LIMIT_BYTES = 1_536
_TECHNICAL_EVIDENCE_LIMIT_BYTES = 1_536
_APPLE_EVIDENCE_LIMIT_BYTES = 2 * 1024
_CAPTURE_FACTS_LIMIT_BYTES = 2 * 1024
_SIMILARITY_EVIDENCE_LIMIT_BYTES = 2_560
_HIGHLIGHT_EVIDENCE_LIMIT_BYTES = 2 * 1024
_FAMILY_CONTEXT_LIMIT_BYTES = 1_536
_WITHHELD_EVIDENCE_KEYS = frozenset(
    {
        "effective_rating",
        "human_decision",
        "human_keywords",
        "human_label",
        "human_notes",
        "human_reason",
        "human_rating",
        "immich_rating",
        "immich_star_rating",
        "keywords",
        "library_points",
        "library_position",
        "notes",
        "owner",
        "owner_label",
        "owner_rating",
        "owner_reason",
        "personal_score",
        "personalized_rating",
        "rating_source",
        "student_score",
        "user_rating",
        "current_effective_rating",
    }
)
_WITHHELD_EVIDENCE_COMPACT_KEYS = frozenset(key.replace("_", "") for key in _WITHHELD_EVIDENCE_KEYS)
# Retained proposal spellings of the star contract's explicit sensitive classes.
EXPLICIT_SENSITIVE_CLASSES = frozenset(
    {
        "suggestive",
        "sexually_explicit",
        "intimate",
        "nude",
        "sexual",
        "erotic",
        "boudoir",
    }
)
ACTIVE_TASK_STATES = {
    TaskState.QUEUED,
    TaskState.RETRY_WAITING,
    TaskState.RUNNING,
    TaskState.CANCEL_REQUESTED,
}


def _valid_star(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5:
        return value
    return None


def normalized_exceptions(value: dict[str, Any] | None) -> dict[str, bool]:
    defaults = DeepReviewExceptionPolicy().model_dump()
    if value:
        for key in defaults:
            if key in value:
                defaults[key] = bool(value[key])
    return defaults


def policy_summary(minimum_rating: int, exceptions: dict[str, bool]) -> str:
    del minimum_rating
    enabled = [key.replace("_", " ") for key, value in exceptions.items() if value]
    base = (
        "Ministral 3 8B rates every photo (at most four stars; a five is a nomination). "
        "The Advanced review (Qwen3.8 27B) runs automatically for every 4- or 5-star "
        "candidate, for uncertain-sensitive photos, and for protected, contradictory, "
        "requested, or sampled 3-star photos; it decides the final star and is the only "
        "automated five-star authority. An explicit sensitive class rated 3 or below is "
        "blurred without escalation and never lowers a star. Qwen3-VL only orders the "
        "4- and 5-star tiers for Highlights afterwards."
    )
    if not enabled:
        return base + " No extra priority signals are enabled."
    return base + " Extra priority signals: " + ", ".join(enabled) + "."


def _policy_document(program: LibraryProgram) -> dict[str, object]:
    return {
        "pipeline_version": program.pipeline_version,
        "sync_interval_seconds": program.sync_interval_seconds,
        "queue_low_watermark": program.queue_low_watermark,
        "queue_high_watermark": program.queue_high_watermark,
        "max_batch_assets": program.max_batch_assets,
        "completion_batch_size": program.completion_batch_size,
        "cohort_mode": program.cohort_mode,
        "selection_order": program.selection_order,
        "ingest_new_assets": program.ingest_new_assets,
        "deep_review_min_rating": program.deep_review_min_rating,
        "deep_review_exceptions": program.deep_review_exceptions,
        "active_deep_review_prompt": program.active_deep_review_prompt,
        "model_pipeline_policy": program.model_pipeline_policy,
    }


async def update_deep_review_policy(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    payload: DeepReviewPolicyUpdate,
    actor_id: uuid.UUID,
) -> LibraryProgram:
    if program.revision != payload.expected_revision:
        raise RuntimeError("deep-review policy changed; reload before saving")
    program.deep_review_min_rating = 4
    program.deep_review_exceptions = payload.exceptions.model_dump()
    program.model_pipeline_policy = {
        **dict(program.model_pipeline_policy or {}),
        "review_contract": REVIEW_CONTRACT_VERSION,
        "apple_advisory_model": payload.apple_advisory_model,
        "nvidia_primary_model": payload.nvidia_primary_model,
        "audit_sample_percent": payload.audit_sample_percent,
        "discretionary_queue_limit": payload.discretionary_queue_limit,
    }
    next_version = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(LibraryPolicyVersion.version), 0)).where(
                    LibraryPolicyVersion.program_id == program.id
                )
            )
            or 0
        )
        + 1
    )
    program.revision += 1
    policy = LibraryPolicyVersion(
        program_id=program.id,
        version=next_version,
        policy_digest=canonical_digest(_policy_document(program)),
        policy=_policy_document(program),
        created_by=actor_id,
    )
    session.add(policy)
    await session.flush()
    program.active_policy_version_id = policy.id
    return program


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def reasons_from_proposal(
    value: dict[str, Any],
    *,
    minimum_primary_rating: int,
    exceptions: dict[str, bool],
    sampled_three_star: bool = False,
) -> list[str]:
    rating = value.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        return []
    reasons: list[str] = []
    sensitive_content = value.get("sensitive_content")
    explicit_sensitive = sensitive_content in EXPLICIT_SENSITIVE_CLASSES
    uncertain = privacy_uncertain(value)
    privacy_observation = value.get("privacy_observation")
    privacy_confidence = (
        privacy_observation.get("privacy_confidence")
        if isinstance(privacy_observation, dict)
        else None
    )
    low_confidence = (
        isinstance(privacy_confidence, int)
        and not isinstance(privacy_confidence, bool)
        and privacy_confidence < PRIVACY_UNCERTAIN_CONFIDENCE
    )
    # A model 5 is published as a provisional 4; the nomination still routes
    # the photo as a five-star candidate so the Advanced review decides first.
    five_star_candidate = rating == 5 or value.get("provisional_five_candidate") is True
    strong = rating >= minimum_primary_rating or five_star_candidate
    # An explicit sensitive class rated 3 or below is already enough for
    # blurring; it escalates only with the star (4 or more). An undecided
    # class, including a "safe" answer with weak typed confidence, always gets
    # the second local visual reviewer. Neither path changes the star.
    if uncertain or (explicit_sensitive and strong):
        reasons.append("nsfw_privacy_review")
    if low_confidence:
        reasons.append("privacy_low_confidence")
    if five_star_candidate:
        reasons.append("five_star_highlight")
    elif rating >= minimum_primary_rating:
        reasons.append("four_star_candidate")
    elif rating == 3 and sampled_three_star:
        reasons.append("three_star_audit_sample")
    if exceptions["privacy_ambiguity"] and uncertain:
        reasons.append("privacy_uncertain")
    if (
        exceptions.get("protected_three_star", True)
        and rating == 3
        and (
            _number(value.get("visible_people_count")) > 0
            or value.get("has_children") is True
            or _number(value.get("family_value")) >= 0.40
            or _number(value.get("documentary_value")) >= 0.60
            or value.get("preserve_despite_technical_faults") is True
            or value.get("recoverability") in {"moderate", "high"}
        )
    ):
        reasons.append("three_star_protected_memory")
    if exceptions["ordinary_three_star"] and rating == 3 and rating < minimum_primary_rating:
        reasons.append("three_star_second_opinion")
    return reasons


def _latest_primary_statement(*, cutoff: datetime) -> Any:
    newer = aliased(AnalysisRun)
    return (
        select(
            AnalysisRun,
            Asset,
            AssetRevision,
            Proposal,
        )
        .join(Asset, Asset.id == AnalysisRun.asset_id)
        .join(AssetRevision, AssetRevision.id == AnalysisRun.asset_revision_id)
        .outerjoin(AnalysisGeneration, AnalysisGeneration.id == AnalysisRun.analysis_generation_id)
        .join(
            Proposal,
            and_(
                Proposal.target_type == "asset",
                Proposal.target_id == Asset.id,
                Proposal.value["analysis_run_id"].as_string() == cast(AnalysisRun.id, String),
                Proposal.value["source"].as_string() == "primary_photo_review",
            ),
        )
        .where(
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
            AnalysisRun.completed_at.is_not(None),
            AnalysisRun.completed_at <= cutoff,
            or_(
                and_(
                    AnalysisRun.analysis_generation_id.is_(None),
                    Asset.current_revision_id == AnalysisRun.asset_revision_id,
                ),
                and_(
                    AnalysisGeneration.state == "published",
                    Asset.current_generation_id == AnalysisGeneration.id,
                ),
            ),
            Asset.media_type == MediaType.IMAGE,
            AssetRevision.is_trashed.is_(False),
            ~exists(
                select(newer.id).where(
                    newer.asset_id == AnalysisRun.asset_id,
                    newer.asset_revision_id == AnalysisRun.asset_revision_id,
                    newer.analysis_type == "primary_review",
                    newer.status == "succeeded",
                    newer.completed_at.is_not(None),
                    newer.completed_at <= cutoff,
                    or_(
                        newer.completed_at > AnalysisRun.completed_at,
                        and_(
                            newer.completed_at == AnalysisRun.completed_at,
                            cast(newer.id, String) > cast(AnalysisRun.id, String),
                        ),
                    ),
                )
            ),
        )
        .order_by(AnalysisRun.completed_at, AnalysisRun.id)
    )


async def preview_backfill_counts(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    coverage: str,
    cutoff: datetime,
) -> dict[str, int]:
    counts = {
        "eligible_count": 0,
        "already_completed_count": 0,
        "queued_or_running_count": 0,
        "missing_count": 0,
        "unavailable_source_count": 0,
    }
    exceptions = normalized_exceptions(program.deep_review_exceptions)
    result = await session.stream(_latest_primary_statement(cutoff=cutoff))
    async for run, asset, _revision, proposal in result:
        reasons = reasons_from_proposal(
            proposal.value,
            minimum_primary_rating=program.deep_review_min_rating,
            exceptions=exceptions,
            sampled_three_star=in_qwen_audit_sample(
                asset.current_revision_id,
                int((program.model_pipeline_policy or {}).get("audit_sample_percent", 0)),
            ),
        )
        if not reasons:
            continue
        counts["eligible_count"] += 1
        if asset.access_state != "available":
            counts["unavailable_source_count"] += 1
            continue
        processor = (
            deep_review_processor_version(program.active_deep_review_prompt)
            if coverage == "refresh_active_prompt"
            else None
        )
        completed_query = select(
            exists().where(
                AnalysisRun.asset_id == asset.id,
                AnalysisRun.asset_revision_id == run.asset_revision_id,
                AnalysisRun.analysis_type == "photo_deep_review",
                AnalysisRun.status == "succeeded",
                *([AnalysisRun.processor_version == processor] if processor is not None else []),
            )
        )
        if bool(await session.scalar(completed_query)):
            counts["already_completed_count"] += 1
            continue
        queued = await session.scalar(
            select(
                exists().where(
                    Task.asset_id == asset.id,
                    Task.asset_revision_id == run.asset_revision_id,
                    Task.task_type == "asset.deep_review",
                    Task.state.in_({*ACTIVE_TASK_STATES, TaskState.PLANNED}),
                    *(
                        [
                            Task.target_ref["prompt_version"].as_string()
                            == program.active_deep_review_prompt
                        ]
                        if coverage == "refresh_active_prompt"
                        else []
                    ),
                )
            )
        )
        if bool(queued):
            counts["queued_or_running_count"] += 1
        else:
            counts["missing_count"] += 1
    return counts


def create_backfill_token(
    settings: Settings,
    *,
    actor_id: uuid.UUID,
    program: LibraryProgram,
    coverage: str,
    cutoff: datetime,
    counts: dict[str, int],
) -> tuple[str, datetime]:
    expires_at = utc_now() + BACKFILL_TOKEN_TTL
    payload = {
        "actor_id": str(actor_id),
        "program_id": str(program.id),
        "policy_revision": program.revision,
        "coverage": coverage,
        "cutoff": cutoff.isoformat(),
        "counts": counts,
        "minimum_primary_rating": program.deep_review_min_rating,
        "exceptions": normalized_exceptions(program.deep_review_exceptions),
        "prompt_version": program.active_deep_review_prompt,
        "expires_at": expires_at.isoformat(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    signature = hmac.new(
        settings.require_session_secret().encode(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}", expires_at


def verify_backfill_token(
    settings: Settings,
    token: str,
    *,
    actor_id: uuid.UUID,
    program: LibraryProgram,
) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.rsplit(".", 1)
        expected_signature = hmac.new(
            settings.require_session_secret().encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise ValueError("backfill confirmation token is invalid")
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode()))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("backfill confirmation token is invalid") from exc
    if payload.get("actor_id") != str(actor_id) or payload.get("program_id") != str(program.id):
        raise ValueError("backfill confirmation token does not match this account")
    if int(payload.get("policy_revision", 0)) != program.revision:
        raise ValueError("deep-review policy changed; preview the backfill again")
    if expires_at <= utc_now():
        raise ValueError("backfill confirmation token expired")
    return dict(payload)


async def start_backfill(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    actor_id: uuid.UUID,
    idempotency_key: str,
    token_payload: dict[str, Any],
) -> DeepReviewBackfill:
    await lock_operation_family(
        session,
        family=f"deep-review-backfill:{program.id}",
        actor_id=actor_id,
    )
    existing = await session.scalar(
        select(DeepReviewBackfill).where(DeepReviewBackfill.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    counts = dict(token_payload.get("counts") or {})
    backfill = DeepReviewBackfill(
        program_id=program.id,
        created_by=actor_id,
        idempotency_key=idempotency_key,
        state="running",
        coverage=str(token_payload["coverage"]),
        policy_revision=int(token_payload["policy_revision"]),
        minimum_primary_rating=int(token_payload["minimum_primary_rating"]),
        exception_policy=normalized_exceptions(dict(token_payload["exceptions"])),
        prompt_version=str(token_payload["prompt_version"]),
        selection_cutoff=datetime.fromisoformat(str(token_payload["cutoff"])),
        eligible_count=int(counts.get("eligible_count", 0)),
    )
    session.add(backfill)
    await session.flush()
    return backfill


async def ensure_active_policy_backfill(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    actor_id: uuid.UUID | None = None,
) -> DeepReviewBackfill | None:
    """Register one durable reconciliation pass for the current run and policy revision."""

    # A finite job used to disqualify itself here, which is why no automatic
    # Advanced backfill has ever run: the helper existed and nothing could
    # reach it. A batch that has finished its first pass is exactly when this
    # pass is wanted, because every photograph rated four or better before the
    # Advanced review existed is still waiting for its second look.
    if not program.enabled or program.state != LibraryProgramState.RUNNING:
        return None
    requested = requested_run_count(program)
    if requested is not None:
        # "Finished doing what it can", not "every photograph settled". A batch
        # can end a dozen photographs short for good reasons, and demanding
        # perfect settlement let a handful of stuck photographs block the whole
        # library from ever starting its next pass.
        in_flight = await session.scalar(
            select(Task.id)
            .where(
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.RUNNING,
                    }
                )
            )
            .limit(1)
        )
        if in_flight is not None:
            return None
    elif program.work_scope != "known":
        return None
    idempotency_key = f"library-policy-{program.id}-{program.revision}"
    existing = await session.scalar(
        select(DeepReviewBackfill).where(DeepReviewBackfill.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    if actor_id is None:
        actor_id = await session.scalar(
            select(User.id)
            .where(
                User.role == Role.ADMINISTRATOR,
                User.is_active.is_(True),
            )
            .limit(1)
        )
    if actor_id is None:
        return None
    cutoff = utc_now()
    return await start_backfill(
        session,
        program=program,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        token_payload={
            "coverage": "missing_any",
            "policy_revision": program.revision,
            "minimum_primary_rating": program.deep_review_min_rating,
            "exceptions": program.deep_review_exceptions,
            "prompt_version": program.active_deep_review_prompt,
            "cutoff": cutoff.isoformat(),
            "counts": {
                "eligible_count": 0,
                "already_completed_count": 0,
                "queued_or_running_count": 0,
                "missing_count": 0,
            },
        },
    )


async def materialize_next_deep_review_backfill(session: AsyncSession) -> bool:
    backfill = await session.scalar(
        select(DeepReviewBackfill)
        .where(DeepReviewBackfill.state.in_({"running", "draining"}))
        .order_by(DeepReviewBackfill.created_at, DeepReviewBackfill.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if backfill is None:
        return False
    program = await session.get(LibraryProgram, backfill.program_id)
    if program is None:
        backfill.state = "failed"
        backfill.last_error_class = "LibraryProgramMissing"
        backfill.completed_at = utc_now()
        return True
    backfill_key = str(backfill.id)
    task_filter = Task.target_ref["backfill_id"].as_string() == backfill_key
    backfill.completed_task_count = int(
        await session.scalar(
            select(func.count(Task.id)).where(task_filter, Task.state == TaskState.SUCCEEDED)
        )
        or 0
    )
    backfill.failed_task_count = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                task_filter, Task.state.in_({TaskState.FAILED, TaskState.CANCELLED})
            )
        )
        or 0
    )
    hot_count = int(
        await session.scalar(
            select(func.count(Task.id)).where(task_filter, Task.state.in_(ACTIVE_TASK_STATES))
        )
        or 0
    )
    terminal_count = backfill.completed_task_count + backfill.failed_task_count
    if backfill.state == "draining":
        if terminal_count >= backfill.planned_task_count:
            backfill.state = "failed" if backfill.failed_task_count else "complete"
            backfill.completed_at = utc_now()
            return True
        return False
    if hot_count >= BACKFILL_HOT_LIMIT:
        return False
    statement = _latest_primary_statement(cutoff=backfill.selection_cutoff)
    if backfill.cursor_completed_at is not None:
        statement = statement.where(
            or_(
                AnalysisRun.completed_at > backfill.cursor_completed_at,
                and_(
                    AnalysisRun.completed_at == backfill.cursor_completed_at,
                    cast(AnalysisRun.id, String) > str(backfill.cursor_run_id),
                ),
            )
        )
    rows = (await session.execute(statement.limit(BACKFILL_CHUNK_SIZE))).all()
    if not rows:
        backfill.state = "draining"
        if terminal_count >= backfill.planned_task_count:
            backfill.state = "failed" if backfill.failed_task_count else "complete"
            backfill.completed_at = utc_now()
        return True
    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        runtimes=SENIOR_REVIEW_RUNTIMES,
        model_keys=[str(program.model_pipeline_policy.get("apple_advisory_model", "qwen3.8:27b"))],
    )
    if not models:
        backfill.last_error_class = "NoDeepReviewModel"
        return False
    planned = 0
    discover_eligible_count = backfill.idempotency_key.startswith("library-policy-")
    for run, asset, _revision, proposal in rows:
        backfill.cursor_completed_at = run.completed_at
        backfill.cursor_run_id = run.id
        backfill.scanned_count += 1
        from star.services.task_scope import task_generation_is_current

        if not await task_generation_is_current(
            session,
            Task(
                asset_id=asset.id,
                asset_revision_id=run.asset_revision_id,
                analysis_generation_id=run.analysis_generation_id,
                target_ref={},
            ),
        ):
            backfill.skipped_count += 1
            continue
        reasons = reasons_from_proposal(
            proposal.value,
            minimum_primary_rating=backfill.minimum_primary_rating,
            exceptions=normalized_exceptions(backfill.exception_policy),
            sampled_three_star=in_qwen_audit_sample(
                asset.current_revision_id,
                int((program.model_pipeline_policy or {}).get("audit_sample_percent", 0)),
            ),
        )
        if reasons and discover_eligible_count:
            backfill.eligible_count += 1
        if not reasons or asset.access_state != "available" or run.campaign_id is None:
            backfill.skipped_count += 1
            continue
        processor = (
            deep_review_processor_version(backfill.prompt_version)
            if backfill.coverage == "refresh_active_prompt"
            else None
        )
        completed = await session.scalar(
            select(
                exists().where(
                    AnalysisRun.asset_id == asset.id,
                    AnalysisRun.asset_revision_id == run.asset_revision_id,
                    AnalysisRun.analysis_type == "photo_deep_review",
                    AnalysisRun.status == "succeeded",
                    *(
                        [AnalysisRun.processor_version == processor]
                        if processor is not None
                        else []
                    ),
                )
            )
        )
        if bool(completed):
            backfill.skipped_count += 1
            continue
        task = await enqueue_primary_review(
            session,
            asset=asset,
            campaign_id=run.campaign_id,
            model=models[0],
            deep_review=True,
            escalation_reasons=reasons,
            priority=deep_review_priority(reasons),
            prompt_version=backfill.prompt_version,
            policy_revision=backfill.policy_revision,
            queue_origin="backfill",
            backfill_id=backfill.id,
            primary_review=run.result_payload,
            analysis_generation_id=run.analysis_generation_id,
        )
        task.processing_batch_id = None
        task.target_ref = {
            **task.target_ref,
            "apple_queue_category": "four_star",
        }
        if task.target_ref.get("backfill_id") == backfill_key:
            planned += 1
            await refresh_asset_curation_snapshot(session, asset.id)
    backfill.planned_task_count += planned
    backfill.last_error_class = None
    return bool(planned or rows)


PromptExperimentRow = tuple[AnalysisRun, Asset, AssetRevision, Proposal | None]
