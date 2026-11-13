from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.analysis.asset_vlm_submission import (
    NVIDIA_CAPABILITY_PREFERENCE,
    eligible_asset_vlm_models,
)
from star.api.dependencies import Principal, request_id, require_roles
from star.api.routers.analysis_audit import _proposal_value
from star.api.routers.workers._shared import (
    _schedule_allows_work,
    _worker_compatibility_details,
)
from star.core.config import Settings, get_settings
from star.core.crypto import constant_time_equal
from star.core.observability import TELEMETRY
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    AppleVisionEvidence,
    Asset,
    AssetCurationSnapshot,
    AssetMetric,
    ImmichConnection,
    LibraryProgram,
    OperationalEvent,
    ProcessingBatch,
    Proposal,
    ReviewReanalysisRun,
    ServiceHeartbeat,
    Task,
    Worker,
)
from star.db.session import get_session, reset_safe_session, session_factory
from star.domain.enums import (
    HealthStatus,
    LibraryProgramState,
    ProposalState,
    ProposalType,
    Role,
    TaskState,
)
from star.domain.schemas import (
    DiagnosticEventResponse,
    FailureOverviewResponse,
    SystemDiagnosticsResponse,
)
from star.services.api_diagnostics import api_latency_summary
from star.services.api_diagnostics import recent_api_errors as get_recent_api_errors
from star.services.audit import AuditRecord, append_audit_event
from star.services.database_profile import database_activity, database_tables, slow_statements
from star.services.failure_overview import failure_overview
from star.services.library_program import requested_run_count
from star.services.operations import OperationRecord, append_operation_event
from star.services.owner_labels import current_human_rating_labels
from star.services.reanalysis_lineage import (
    reanalysis_generation_ids,
    reanalysis_task_filter,
)
from star.services.storage import get_storage_status

router = APIRouter(tags=["Diagnostics"])
owner = require_roles(Role.ADMINISTRATOR)
logger = logging.getLogger(__name__)

ACTIVE_TASK_STATES = {
    TaskState.PLANNED,
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.RETRY_WAITING,
    TaskState.CANCEL_REQUESTED,
}
REDACTED_EVENT_DETAIL_TERMS = {
    "api_key",
    "authorization",
    "credential",
    "exif",
    "face",
    "filename",
    "header",
    "image",
    "media",
    "ocr",
    "password",
    "path",
    "prompt",
    "response",
    "secret",
    "token",
    "transcript",
}
REDACTED_REVIEW_DETAIL_TERMS = REDACTED_EVENT_DETAIL_TERMS | {
    "base64",
    "bbox",
    "bounding",
    "crop",
    "digest",
    "derivative",
    "face_id",
    "immich_asset_id",
    "input_digest",
    "original",
    "person_id",
    "prompt",
    "raw_response",
    "relative_path",
    "transcript",
}
REDACTED_REVIEW_DETAIL_KEYS = {
    "analysis_run_id",
    "asset_revision_id",
    "campaign_id",
    "connection_id",
    "full_text",
    "generation_id",
    "processing_batch_id",
    "proposal_id",
    "recognized_text",
    "recognize_text",
    "text_content",
    "text_observation",
    "text_observations",
    "task_id",
    "uuid",
    "worker_id",
}
WORKER_RESULT_EVENT_TYPES = {
    "apple_vision.completed",
    "model.fast_rating.completed",
    "model.deep_review.completed",
    "model.highlight_comparison.completed",
    "model.primary_review.completed",
    "model.series_selection.completed",
    "pipeline.benchmark.result_completed",
    "semantic_embedding.completed",
    "task.completed",
}


def _bearer_token(value: str | None) -> str:
    if not value:
        return ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def require_agent_diagnostics(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.get_agent_diagnostics_token()
    if len(expected) < 32:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if (
        settings.agent_diagnostics_require_edge
        and request.headers.get("x-star-edge") != "star-edge"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="edge access required")
    supplied = _bearer_token(authorization)
    if not supplied or not constant_time_equal(expected, supplied):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="diagnostics credential required",
        )


async def require_agent_operations(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Authorize only the fixed, audited agent operation surface."""

    expected = settings.get_agent_operations_token()
    if len(expected) < 32:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if settings.agent_operations_require_edge and request.headers.get("x-star-edge") != "star-edge":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="edge access required")
    supplied = _bearer_token(authorization)
    if not supplied or not constant_time_equal(expected, supplied):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operations credential required",
        )


def _counts(rows: list[tuple[object, int]]) -> dict[str, int]:
    return {getattr(key, "value", str(key)): int(count) for key, count in rows}


def _age_seconds(value: datetime, now: datetime) -> float:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return max(0.0, (now - aware).total_seconds())


def _sanitized_event_detail(value: object, *, depth: int = 0) -> object:
    if depth >= 4:
        return "[nested detail omitted]"
    if isinstance(value, dict):
        return {
            str(key): _sanitized_event_detail(item, depth=depth + 1)
            for key, item in value.items()
            if not any(term in str(key).lower() for term in REDACTED_EVENT_DETAIL_TERMS)
        }
    if isinstance(value, list):
        return [_sanitized_event_detail(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:253] + "..."
    return value


async def _curation_diagnostics(session: AsyncSession) -> dict[str, Any]:
    """Report what the library believes about its own photographs.

    This exists so the star distribution, the reason the final stage has not
    started, and the failure codes actually occurring can be read without shell
    access to the coordinator. Counts and codes only: no filenames, media,
    EXIF, prompts or model prose ever appear here.
    """

    from star.db.models import AssetCurationSnapshot, HighlightLeagueEntry
    from star.services.highlight_league import (
        HIGHLIGHT_GENERATION,
        MAX_SOURCE_RECOVERIES,
        five_star_field_unsettled,
    )

    rating_rows = (
        await session.execute(
            select(AssetCurationSnapshot.effective_rating, func.count())
            .where(AssetCurationSnapshot.analysis_complete.is_(True))
            .group_by(AssetCurationSnapshot.effective_rating)
        )
    ).all()
    by_rating = {str(rating): int(count) for rating, count in rating_rows if rating is not None}
    rated = sum(by_rating.values())
    fives = by_rating.get("5", 0)

    human_rows = (
        await session.execute(
            select(AssetCurationSnapshot.effective_rating, func.count())
            .where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.rating_source.in_({"star_human", "immich_human"}),
            )
            .group_by(AssetCurationSnapshot.effective_rating)
        )
    ).all()
    by_owner = {str(rating): int(count) for rating, count in human_rows if rating is not None}

    league_rows = (
        await session.execute(
            select(HighlightLeagueEntry.five_star_state, func.count())
            .where(HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION)
            .group_by(HighlightLeagueEntry.five_star_state)
        )
    ).all()
    # Photographs the comparisons gave up on: Immich would not serve them often
    # enough, or they spent their compute budget. They never settle, so they are
    # named here rather than counted as work still to do.
    league_retired = int(
        await session.scalar(
            select(func.count(HighlightLeagueEntry.asset_id)).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.source_failure_count >= MAX_SOURCE_RECOVERIES,
            )
        )
        or 0
    )

    from star.db.models import LibraryReviewNote

    latest_review = await session.scalar(
        select(LibraryReviewNote)
        .where(LibraryReviewNote.state == "complete")
        .order_by(LibraryReviewNote.created_at.desc())
        .limit(1)
    )
    advanced_pending = await five_star_field_unsettled(session)
    # Since v240 promotion runs every cycle; what blocks the final stage is
    # whatever the scheduler's own gate last said, not a guess made here.
    scheduler_beat = await session.scalar(
        select(ServiceHeartbeat).where(ServiceHeartbeat.service_key == "scheduler")
    )
    gates = dict(
        (((scheduler_beat.details or {}) if scheduler_beat else {}).get("telemetry", {})).get(
            "gates", {}
        )
    )
    promotion_gate = dict(gates.get("finalize_five_star_tier", {}))
    blocked_by = (
        str(promotion_gate.get("reason"))
        if promotion_gate.get("verdict") == "refused" and promotion_gate.get("reason")
        else None
    )
    return {
        "rated_photos": rated,
        "by_rating": by_rating,
        "by_rating_owner_set": by_owner,
        "five_star_share": round(fives / rated, 5) if rated else 0.0,
        "five_star_owner_set": by_owner.get("5", 0),
        "league_entries_by_state": {
            str(state): int(count) for state, count in league_rows if state is not None
        },
        # Why the final stage is or is not running, which is otherwise invisible.
        "league_entries_retired_for_source": league_retired,
        "advanced_reviews_pending": advanced_pending,
        "highlights_blocked_by": blocked_by,
        # Whether the library has read itself lately and what it asked for, as
        # counts and action names. Its actual words stay out of the operational
        # surface and are read through the owner's own API, because model prose
        # does not belong in a sanitized diagnostics document.
        "last_review": {
            "at": latest_review.completed_at.isoformat()
            if latest_review is not None and latest_review.completed_at is not None
            else None,
            "finding_count": len(latest_review.findings) if latest_review is not None else 0,
            "proposed_actions": [str(item.get("action")) for item in latest_review.proposals]
            if latest_review is not None
            else [],
        },
    }


async def _build_diagnostics(
    session: AsyncSession,
    settings: Settings,
) -> SystemDiagnosticsResponse:
    now = utc_now()
    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    task_counts = _counts(
        list(
            (await session.execute(select(Task.state, func.count()).group_by(Task.state)))
            .tuples()
            .all()
        )
    )
    route_counts = {
        str(route): int(count)
        for route, count in (
            await session.execute(
                select(Task.routing_key, func.count())
                .where(Task.state.in_(ACTIVE_TASK_STATES))
                .group_by(Task.routing_key)
            )
        ).all()
    }
    type_counts = {
        str(task_type): int(count)
        for task_type, count in (
            await session.execute(
                select(Task.task_type, func.count())
                .where(Task.state.in_(ACTIVE_TASK_STATES))
                .group_by(Task.task_type)
            )
        ).all()
    }
    # Which kind of work is queued, which is running and where, and how long
    # the oldest ready task of each kind has waited: the difference between
    # "twelve comparisons active" and "twelve comparisons nobody leases".
    active_by_type_state: dict[str, dict[str, int]] = {}
    for task_type, state, count in (
        await session.execute(
            select(Task.task_type, Task.state, func.count())
            .where(Task.state.in_(ACTIVE_TASK_STATES))
            .group_by(Task.task_type, Task.state)
        )
    ).all():
        active_by_type_state.setdefault(str(task_type), {})[
            getattr(state, "value", str(state)).lower()
        ] = int(count)
    oldest_ready_by_type = {
        str(task_type): round(max(0.0, _age_seconds(oldest, now)), 1)
        for task_type, oldest in (
            await session.execute(
                select(Task.task_type, func.min(Task.available_at))
                .where(
                    Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING}),
                    Task.available_at <= now,
                )
                .group_by(Task.task_type)
            )
        ).all()
        if oldest is not None
    }
    running_by_worker: dict[str, dict[str, int]] = {}
    for label, task_type, count in (
        await session.execute(
            select(Worker.hostname_label, Task.task_type, func.count())
            .join(Worker, Worker.id == Task.leased_to_worker_id)
            .where(Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}))
            .group_by(Worker.hostname_label, Task.task_type)
        )
    ).all():
        running_by_worker.setdefault(str(label), {})[str(task_type)] = int(count)
    active_origin_counts: dict[str, int] = {}
    active_lineage_rows = (
        await session.execute(
            select(Task.target_ref, Task.analysis_generation_id).where(
                Task.state.in_(ACTIVE_TASK_STATES)
            )
        )
    ).all()
    for target_ref, analysis_generation_id in active_lineage_rows:
        ref = target_ref if isinstance(target_ref, dict) else {}
        if ref.get("reanalysis_run_id") or ref.get("manual_run_id"):
            origin = "reanalysis"
        elif ref.get("experiment_id"):
            origin = "deep_curation"
        elif ref.get("queue_origin") == "user" or ref.get("user_request_reason"):
            origin = "owner_requested"
        elif analysis_generation_id is not None:
            origin = "review_pipeline"
        else:
            origin = "supporting_work"
        active_origin_counts[origin] = active_origin_counts.get(origin, 0) + 1
    failure_counts = {
        str(error_class or "UnknownFailure"): int(count)
        for error_class, count in (
            await session.execute(
                select(Task.last_error_class, func.count())
                .where(Task.state == TaskState.FAILED)
                .group_by(Task.last_error_class)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
    }
    recent_failed_tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.state == TaskState.FAILED)
                .order_by(Task.updated_at.desc(), Task.id.desc())
                .limit(50)
            )
        ).all()
    )
    connection_counts = _counts(
        list(
            (
                await session.execute(
                    select(ImmichConnection.health, func.count()).group_by(ImmichConnection.health)
                )
            )
            .tuples()
            .all()
        )
    )
    connection_rows = list(
        (
            await session.scalars(select(ImmichConnection).order_by(ImmichConnection.created_at))
        ).all()
    )
    eligible_models = await eligible_asset_vlm_models(session, limit=100)
    primary_models = [
        model
        for model in eligible_models
        if str(model.resource_profile.get("capability", "small-vlm"))
        in NVIDIA_CAPABILITY_PREFERENCE
    ]
    worker_rows = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).order_by(Worker.hostname_label)
            )
        ).all()
    )
    connected_workers = [
        worker
        for worker in worker_rows
        if _age_seconds(worker.last_heartbeat, now) <= settings.worker_stale_seconds
    ]
    runnable_workers = [
        worker for worker in connected_workers if worker.state in {"active", "draining"}
    ]
    primary_workers = [
        worker for worker in runnable_workers if worker.capabilities.get("small-vlm") is True
    ]
    worker_activity: dict[object, dict[str, dict[str, object] | None]] = {
        worker.id: {"last_lease": None, "last_result": None} for worker in worker_rows
    }
    for worker in worker_rows:
        for key, event_types in (
            ("last_lease", {"task.leased"}),
            ("last_result", WORKER_RESULT_EVENT_TYPES),
        ):
            row = (
                await session.execute(
                    select(OperationalEvent, Task.routing_key)
                    .outerjoin(Task, Task.id == OperationalEvent.task_id)
                    .where(
                        OperationalEvent.worker_id == worker.id,
                        OperationalEvent.event_type.in_(event_types),
                    )
                    .order_by(OperationalEvent.id.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                continue
            event, route = row
            worker_activity[worker.id][key] = {
                "created_at": event.created_at,
                "event_type": event.event_type,
                "route": str(route or event.details.get("routing_key") or "unknown"),
            }
    scheduler = await session.scalar(
        select(ServiceHeartbeat).where(ServiceHeartbeat.service_key == "scheduler")
    )
    scheduler_online = bool(
        scheduler
        and _age_seconds(scheduler.last_heartbeat, now) <= settings.service_stale_seconds
        and scheduler.status == "online"
    )
    recent_rows = list(
        (
            await session.scalars(
                select(OperationalEvent).order_by(OperationalEvent.id.desc()).limit(50)
            )
        ).all()
    )
    recent_events = [
        DiagnosticEventResponse(
            severity=event.severity,
            source=event.source,
            event_type=event.event_type,
            message=event.message,
            details=_sanitized_event_detail(event.details),
            created_at=event.created_at,
        )
        for event in recent_rows
    ]
    last_sync = next(
        (event for event in recent_rows if event.event_type == "immich.sync.completed"),
        None,
    )
    active_tasks = sum(task_counts.get(state.value, 0) for state in ACTIVE_TASK_STATES)
    program_state = program.state if program is not None else LibraryProgramState.DISABLED
    healthy_connections = connection_counts.get(HealthStatus.HEALTHY.value, 0)
    storage = await get_storage_status(session, settings)
    reanalysis = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.state.in_({"running", "draining", "paused"}))
        .order_by(ReviewReanalysisRun.created_at, ReviewReanalysisRun.id)
        .limit(1)
    )
    if reanalysis is None:
        reanalysis = await session.scalar(
            select(ReviewReanalysisRun)
            .order_by(ReviewReanalysisRun.created_at.desc(), ReviewReanalysisRun.id.desc())
            .limit(1)
        )
    reanalysis_progress: dict[str, object] = {}
    if reanalysis is not None:
        requested = reanalysis.selection.get("_requested_photo_count")
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
            requested = max(
                reanalysis.scanned_count,
                reanalysis.planned_task_count + reanalysis.skipped_count,
                reanalysis.completed_task_count
                + reanalysis.failed_task_count
                + reanalysis.excluded_task_count,
            )
        reanalysis_active_tasks = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    reanalysis_task_filter(reanalysis.id),
                    Task.state.in_(ACTIVE_TASK_STATES),
                )
            )
            or 0
        )
        # Why the unsettled photographs are unsettled. A wave whose machines
        # are busy but whose photographs never publish looks identical from the
        # outside to one that is simply slow, and the difference is the whole
        # diagnosis: these two breakdowns name the generation states and the
        # exact node each stalled photograph is still waiting on.
        generation_ids = reanalysis_generation_ids(reanalysis.id)
        generation_states = {
            str(state): int(count)
            for state, count in (
                await session.execute(
                    select(AnalysisGeneration.state, func.count())
                    .where(AnalysisGeneration.id.in_(generation_ids))
                    .group_by(AnalysisGeneration.state)
                )
            ).all()
            if state
        }
        # What became of the discarded ones. A wave that keeps replacing its own
        # work and one whose photographs move underneath it look identical
        # without this.
        superseded_reasons = {
            str(reason): int(count)
            for reason, count in (
                await session.execute(
                    select(AnalysisGeneration.failure_class, func.count())
                    .where(
                        AnalysisGeneration.id.in_(generation_ids),
                        AnalysisGeneration.state == "superseded",
                    )
                    .group_by(AnalysisGeneration.failure_class)
                )
            ).all()
        }
        blocking_nodes = {
            f"{stage}:{state}": int(count)
            for stage, state, count in (
                await session.execute(
                    select(AnalysisNode.stage_key, AnalysisNode.state, func.count())
                    .join(
                        AnalysisGeneration,
                        AnalysisGeneration.id == AnalysisNode.generation_id,
                    )
                    .where(
                        AnalysisNode.generation_id.in_(generation_ids),
                        AnalysisGeneration.state.in_({"building", "complete_unpublished"}),
                        AnalysisNode.required.is_(True),
                        AnalysisNode.applicable.is_(True),
                        AnalysisNode.state != "succeeded",
                    )
                    .group_by(AnalysisNode.stage_key, AnalysisNode.state)
                )
            ).all()
            if stage
        }
        # Two runs fighting over the same photographs would look, from one
        # run's counters, exactly like one run circling. Name them all.
        runs_by_state = {
            str(state): int(count)
            for state, count in (
                await session.execute(
                    select(ReviewReanalysisRun.state, func.count()).group_by(
                        ReviewReanalysisRun.state
                    )
                )
            ).all()
        }
        distinct_photographs = int(
            await session.scalar(
                select(func.count(func.distinct(AnalysisGeneration.asset_id))).where(
                    AnalysisGeneration.id.in_(generation_ids)
                )
            )
            or 0
        )
        reanalysis_progress = {
            "state": reanalysis.state,
            "queued_advanced_by_currency": await _queued_advanced_by_currency(session),
            "runs_by_state": runs_by_state,
            # Scans against photographs: a wave that circles scans thousands of
            # times more than it touches.
            "distinct_photographs": distinct_photographs,
            "generation_states": generation_states,
            "superseded_reasons": superseded_reasons,
            "unsettled_waiting_on": blocking_nodes,
            # Whether the wave is moving forward through the library at all.
            "cursor_at": (
                reanalysis.cursor_created_at.isoformat()
                if reanalysis.cursor_created_at is not None
                else None
            ),
            "mode": reanalysis.mode,
            "start_stage": reanalysis.start_stage,
            "publication_policy": reanalysis.publication_policy,
            "requested_photos": requested,
            "scanned_photos": reanalysis.scanned_count,
            "materialized_photos": reanalysis.planned_task_count,
            "published_photos": reanalysis.completed_task_count,
            "failed_photos": reanalysis.failed_task_count,
            "skipped_photos": reanalysis.skipped_count,
            "excluded_photos": reanalysis.excluded_task_count,
            "selection_remaining": max(0, requested - reanalysis.scanned_count),
            "materialized_unsettled": max(
                0,
                reanalysis.planned_task_count
                - reanalysis.completed_task_count
                - reanalysis.failed_task_count
                - reanalysis.excluded_task_count,
            ),
            "active_tasks": reanalysis_active_tasks,
            "created_at": reanalysis.created_at,
            "completed_at": reanalysis.completed_at,
        }

    required_photo_review_active = type_counts.get("asset.primary_review", 0) > 0
    loop_details = scheduler.details.get("loops", {}) if scheduler else {}
    failing_loops = (
        [
            name
            for name, state in loop_details.items()
            if isinstance(state, dict) and state.get("consecutive_failures", 0)
        ]
        if isinstance(loop_details, dict)
        else []
    )
    if not scheduler_online and (program_state != LibraryProgramState.DISABLED or active_tasks > 0):
        diagnostic_status = "attention"
        summary = "Durable work exists but the scheduler heartbeat is offline."
        next_action = "Inspect or restart the NAS scheduler service."
    elif failing_loops:
        diagnostic_status = "attention"
        summary = "Scheduler subsystems are retrying: " + ", ".join(sorted(failing_loops)) + "."
        next_action = "Inspect scheduler diagnostics; online workers alone do not prove progress."
    elif healthy_connections == 0 and (
        program_state != LibraryProgramState.DISABLED or required_photo_review_active
    ):
        diagnostic_status = "attention"
        summary = "Durable work exists but no healthy Immich connection is available."
        next_action = "Open Immich settings and verify the read-only connection."
    elif required_photo_review_active and not primary_models:
        diagnostic_status = "attention"
        summary = "No eligible NVIDIA photo-review model is registered."
        next_action = "Inspect Models and restore the trusted Ministral NVIDIA registration."
    elif required_photo_review_active and not primary_workers:
        diagnostic_status = "attention"
        summary = "Photo reviews are queued but no connected small-vlm worker is allowed to lease."
        next_action = (
            "Inspect connectivity, desired state, schedule, and compatibility on Machines."
        )
    elif active_tasks:
        diagnostic_status = "working"
        if program_state == LibraryProgramState.DISABLED:
            summary = (
                f"New photo intake is stopped; {active_tasks} retained re-review or supporting "
                "jobs are still processing."
            )
            next_action = "No new photos are being admitted; watch Queue for the retained work."
        else:
            summary = f"Star is actively processing {active_tasks} queued or running jobs."
            next_action = "No action is required; watch Processing or Activity for progress."
    elif program_state == LibraryProgramState.DISABLED:
        diagnostic_status = "stopped"
        summary = "Photo curation is stopped; idle worker lease responses are expected."
        next_action = "Start a bounded photo run from Immich when you want more work admitted."
    elif last_sync is not None and int(last_sync.details.get("photos", 0) or 0) == 0:
        diagnostic_status = "working"
        summary = "The scheduler is scanning past already-known Immich records for eligible work."
        next_action = "No worker action is needed; the next bounded metadata page will continue."
    else:
        diagnostic_status = "working"
        summary = "The program is running and waiting for the coordinator's next admission cycle."
        next_action = "If this remains unchanged, copy this report and inspect recent events."

    return SystemDiagnosticsResponse(
        generated_at=now,
        software_version=__version__,
        release_version=settings.image_tag,
        build_provenance={
            "git_sha": settings.build_git_sha,
            "git_tree_state": settings.build_tree_state,
            "source_fingerprint": settings.build_source_fingerprint,
        },
        status=diagnostic_status,
        summary=summary,
        next_action=next_action,
        privacy_notice=(
            "Sanitized operational metadata only: no filenames, media, EXIF, OCR, prompts, "
            "model responses, credentials, or arbitrary database rows."
        ),
        scheduler={
            "status": "online" if scheduler_online else "offline",
            "last_heartbeat": scheduler.last_heartbeat if scheduler else None,
            "heartbeat_age_seconds": (
                round(_age_seconds(scheduler.last_heartbeat, now), 1) if scheduler else None
            ),
            "supervision": (scheduler.details.get("supervision") if scheduler else None),
            # The scheduler's own build identity, reported by that process. It
            # can differ from this API's when a container was not recreated.
            "software_version": (scheduler.software_version if scheduler else None),
            "release_version": (scheduler.details.get("release_version") if scheduler else None),
            "build_provenance": (
                scheduler.details.get("build_provenance", {}) if scheduler else {}
            ),
            "loops": scheduler.details.get("loops", {}) if scheduler else {},
            # What the scheduler decided and how long each phase took, so a
            # refusing gate or a phase eating the cycle is named, not inferred.
            "telemetry": scheduler.details.get("telemetry", {}) if scheduler else {},
        },
        library_program={
            "state": program_state.value,
            "enabled": bool(program and program.enabled),
            "requested_asset_count": requested_run_count(program) if program else None,
            "original_requested_asset_count": (
                program.original_requested_asset_count if program else None
            ),
            "admitted_asset_count": program.admitted_asset_count if program else 0,
            "finalized_asset_count": program.finalized_asset_count if program else 0,
            "selection_order": program.selection_order if program else None,
            "cohort_mode": program.cohort_mode if program else None,
            "completion_batch_size": program.completion_batch_size if program else None,
            "source_cursor": program.source_cursor if program else 0,
            "ingest_new_assets": bool(program and program.ingest_new_assets),
            "last_planned_at": program.last_planned_at if program else None,
            "last_error_class": program.last_error_class if program else None,
        },
        database={
            "status": "online",
            "assets": int(await session.scalar(select(func.count()).select_from(Asset)) or 0),
            "fully_reviewed": int(
                await session.scalar(
                    select(func.count())
                    .select_from(AssetCurationSnapshot)
                    .where(AssetCurationSnapshot.analysis_complete.is_(True))
                )
                or 0
            ),
            "analysis_runs": int(
                await session.scalar(select(func.count()).select_from(AnalysisRun)) or 0
            ),
            "processing_batches": int(
                await session.scalar(select(func.count()).select_from(ProcessingBatch)) or 0
            ),
            "operational_events": int(
                await session.scalar(select(func.count()).select_from(OperationalEvent)) or 0
            ),
            # Building generations of photographs that already have a current
            # one are the re-review pass still to do: the number that should
            # fall, where the queue depth only shows the backfill's runway.
            "generation_states": {
                str(state): int(count)
                for state, count in (
                    await session.execute(
                        select(AnalysisGeneration.state, func.count()).group_by(
                            AnalysisGeneration.state
                        )
                    )
                ).all()
            },
        },
        queue={
            "active": active_tasks,
            "state_count_scope": "all_retained_task_history",
            "active_count_scope": "currently_runnable_or_leased_work",
            "by_state": task_counts,
            "active_by_route": route_counts,
            "active_by_type": type_counts,
            "active_by_type_state": active_by_type_state,
            "oldest_ready_seconds_by_type": oldest_ready_by_type,
            "running_by_worker": running_by_worker,
            "active_by_origin": active_origin_counts,
            "failures_by_class": failure_counts,
            "recent_failures": [
                {
                    "task_type": task.task_type,
                    "route": task.routing_key,
                    "error_class": task.last_error_class or "UnknownFailure",
                    "attempt": task.attempt_count,
                    "maximum_attempts": task.max_attempts,
                    "recovery_stage": str((task.progress or {}).get("stage", "terminal")),
                    "updated_at": task.updated_at,
                }
                for task in recent_failed_tasks
            ],
        },
        reanalysis=reanalysis_progress,
        workers={
            "online": len(connected_workers),
            "enrolled": len(worker_rows),
            "primary_review_online": len(primary_workers),
            "primary_review_connected": len(
                [
                    worker
                    for worker in connected_workers
                    if worker.capabilities.get("small-vlm") is True
                ]
            ),
            "primary_review_runnable": len(primary_workers),
            "labels": [worker.hostname_label for worker in connected_workers],
            "items": [
                {
                    "label": worker.hostname_label,
                    "online": worker in connected_workers,
                    "heartbeat_at": worker.last_heartbeat,
                    "heartbeat_age_seconds": round(_age_seconds(worker.last_heartbeat, now), 1),
                    "release_version": str(
                        worker.runtime_status.get("release_version", "unversioned")
                    ),
                    "build_git_sha": str(worker.runtime_status.get("build_git_sha", "unknown")),
                    "build_tree_state": str(
                        worker.runtime_status.get("build_tree_state", "unknown")
                    ),
                    "build_source_fingerprint": str(
                        worker.runtime_status.get("build_source_fingerprint", "unknown")
                    ),
                    "build_provenance": {
                        "git_sha": str(worker.runtime_status.get("build_git_sha", "unknown")),
                        "git_tree_state": str(
                            worker.runtime_status.get("build_tree_state", "unknown")
                        ),
                        "source_fingerprint": str(
                            worker.runtime_status.get("build_source_fingerprint", "unknown")
                        ),
                    },
                    "expected_release_version": settings.image_tag,
                    **_worker_compatibility_details(worker.runtime_status, settings),
                    "desired_state": worker.desired_state,
                    "schedule_active": _schedule_allows_work(worker.schedule_policy, now=now),
                    "accepted_queues": sorted(
                        str(route) for route in worker.runtime_status.get("accepted_queues", [])
                    ),
                    "maximum_concurrency": int(
                        worker.runtime_status.get("maximum_concurrency", 1) or 1
                    ),
                    "resident_model_keys": sorted(
                        str(model) for model in worker.runtime_status.get("resident_model_keys", [])
                    ),
                    "runtime_phase": str(worker.runtime_status.get("runtime_phase", "idle")),
                    "runtime_model_key": worker.runtime_status.get("runtime_model_key"),
                    "battery_admission_state": worker.runtime_status.get("battery_admission_state")
                    if str(worker.runtime_status.get("battery_admission_state"))
                    in {
                        "full",
                        "unrestricted",
                        "unplugged",
                        "charging_to_resume",
                        "paused_low_battery",
                        "telemetry_unavailable",
                    }
                    else None,
                    **worker_activity[worker.id],
                }
                for worker in worker_rows
            ],
        },
        models={
            "eligible_local_vision": len(eligible_models),
            "eligible_primary_review": len(primary_models),
            "primary_model_keys": [model.model_id for model in primary_models],
        },
        connections={
            "by_health": connection_counts,
            "healthy": healthy_connections,
            "items": [
                {
                    "health": getattr(connection.health, "value", str(connection.health)),
                    "last_sync_at": connection.last_sync_at,
                    "sync_mode": connection.read_policy.get("sync_mode", "unknown"),
                    "missing_recommended_permissions": connection.read_policy.get(
                        "missing_recommended_permissions", []
                    ),
                    "sync_stream_missing_permissions": connection.read_policy.get(
                        "sync_stream_missing_permissions", []
                    ),
                }
                for connection in connection_rows
            ],
            "last_sync": {
                "created_at": last_sync.created_at if last_sync else None,
                "details": (_sanitized_event_detail(last_sync.details) if last_sync else {}),
            },
        },
        storage=storage.model_dump(mode="json"),
        curation=await _curation_diagnostics(session),
        recent_events=recent_events,
        recent_api_errors=get_recent_api_errors(limit=50),
        api_latency=api_latency_summary(),
        database_activity=await database_activity(session),
        database_tables=await database_tables(session),
        slow_statements=await slow_statements(session),
        # The lease path's own gates (model turn, obsolete leases, refusals)
        # live in the API process, which the scheduler heartbeat never carries.
        api_gates=TELEMETRY.snapshot().get("gates", {}),
    )


async def _degraded_diagnostics(
    session: AsyncSession,
    settings: Settings,
    exc: Exception,
    *,
    route: str,
) -> SystemDiagnosticsResponse:
    """Keep the safe diagnostic channel available when one snapshot query fails."""

    await session.rollback()
    record = {
        "created_at": utc_now(),
        "method": "GET",
        "route": route,
        "status_code": 200,
        "error_class": type(exc).__name__,
        "request_id": "diagnostics-snapshot",
    }
    logger.exception("agent_diagnostics_snapshot_degraded")
    return SystemDiagnosticsResponse(
        generated_at=utc_now(),
        software_version=__version__,
        release_version=settings.image_tag,
        build_provenance={
            "git_sha": settings.build_git_sha,
            "git_tree_state": settings.build_tree_state,
            "source_fingerprint": settings.build_source_fingerprint,
        },
        status="attention",
        summary="The safe diagnostic snapshot is rebuilding after a transient read failure.",
        next_action="No owner action is required; Star will retry on the next request.",
        privacy_notice=(
            "Sanitized operational metadata only: no filenames, media, EXIF, OCR, prompts, "
            "model responses, credentials, or arbitrary database rows."
        ),
        scheduler={"status": "unknown", "loops": {}},
        library_program={"state": "unknown", "last_error_class": type(exc).__name__},
        database={"status": "degraded_snapshot"},
        queue={"recent_failures": [], "failures_by_class": {}},
        reanalysis={},
        workers={"online": 0, "enrolled": 0, "items": []},
        models={"eligible_local_vision": 0, "eligible_primary_review": 0},
        connections={"healthy": 0, "items": []},
        storage={"status": "unknown"},
        recent_events=[],
        recent_api_errors=[record, *get_recent_api_errors(limit=49)],
    )


@router.get("/system/diagnostics", response_model=SystemDiagnosticsResponse)
async def system_diagnostics(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SystemDiagnosticsResponse:
    try:
        return await _build_diagnostics(session, settings)
    except Exception as exc:
        return await _degraded_diagnostics(
            session,
            settings,
            exc,
            route="/api/v1/system/diagnostics",
        )


@router.get(
    "/agent/diagnostics",
    response_model=SystemDiagnosticsResponse,
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_diagnostics(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SystemDiagnosticsResponse:
    try:
        return await _build_diagnostics(session, settings)
    except Exception as exc:
        return await _degraded_diagnostics(
            session,
            settings,
            exc,
            route="/api/v1/agent/diagnostics",
        )


@router.get(
    "/agent/failures",
    response_model=FailureOverviewResponse,
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_failures(
    hours: int = Query(default=72, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
) -> FailureOverviewResponse:
    """Failed work classified for analysis from the terminal (diagnostics token)."""

    return await failure_overview(session, window=timedelta(hours=hours))


@router.get("/failures", response_model=FailureOverviewResponse)
async def owner_failures(
    hours: int = Query(default=72, ge=1, le=720),
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
) -> FailureOverviewResponse:
    """Failed work classified for the Failures page: what, why, whether it matters."""

    return await failure_overview(session, window=timedelta(hours=hours))


@router.post(
    "/agent/operations/reanalysis/retry-failed",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_retry_failed_reanalysis(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Retry failed work in the newest bounded re-analysis run.

    This deliberately exposes no run ID, SQL, shell, arbitrary task mutation,
    media, or prompt data. Replaying it after the failed rows have moved is a
    no-op, and every invocation is retained in audit and Activity history.
    """

    from star.api.routers.reanalysis import _action

    run = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.state.in_({"running", "draining", "paused", "failed"}))
        .order_by(ReviewReanalysisRun.created_at.desc(), ReviewReanalysisRun.id.desc())
        .with_for_update()
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no re-analysis run")
    before_state = run.state
    changed = await _action(session, run, "retry")
    await append_audit_event(
        session,
        AuditRecord(
            event_type="agent.reanalysis.retry_failed",
            actor_type="service_account",
            actor_id="star-agent-operations",
            target_ref={"surface": "latest_reanalysis"},
            correlation_id=correlation_id,
            before={"state": before_state},
            after={"state": run.state, "changed_tasks": changed},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="agent.reanalysis.retry_failed",
            message="Scoped service account retried failed re-analysis work",
            correlation_id=correlation_id,
            details={"changed_tasks": changed, "state": run.state},
        ),
    )
    await session.commit()
    return {
        "operation": "retry_failed_reanalysis",
        "changed_tasks": changed,
        "state": run.state,
        "message": (
            "Failed re-analysis work was queued again."
            if changed
            else "No failed re-analysis task needed a retry."
        ),
    }


async def _current_agent_reanalysis(session: AsyncSession) -> ReviewReanalysisRun:
    run = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.state.in_({"running", "draining", "paused", "failed"}))
        .order_by(ReviewReanalysisRun.created_at.desc(), ReviewReanalysisRun.id.desc())
        .with_for_update()
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no re-analysis run")
    return run


async def _record_agent_reanalysis_control(
    session: AsyncSession,
    *,
    run: ReviewReanalysisRun,
    operation: str,
    before_state: str,
    changed_tasks: int,
    correlation_id: str,
) -> None:
    event_type = f"agent.reanalysis.{operation}"
    await append_audit_event(
        session,
        AuditRecord(
            event_type=event_type,
            actor_type="service_account",
            actor_id="star-agent-operations",
            target_ref={"surface": "current_reanalysis"},
            correlation_id=correlation_id,
            before={"state": before_state},
            after={"state": run.state, "changed_tasks": changed_tasks},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type=event_type,
            message=f"Scoped service account requested re-analysis {operation}",
            correlation_id=correlation_id,
            details={"changed_tasks": changed_tasks, "state": run.state},
        ),
    )


@router.post(
    "/agent/operations/reanalysis/ensure-complete",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_ensure_reanalysis_complete(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Resume the current bounded review and retry its terminal work.

    The endpoint accepts no target or payload. Normal scheduler reconciliation
    remains authoritative for retry backoff, source quarantine, and publication.
    """

    from star.api.routers.reanalysis import _action

    run = await _current_agent_reanalysis(session)
    before_state = run.state
    changed = await _action(session, run, "retry")
    await _record_agent_reanalysis_control(
        session,
        run=run,
        operation="ensure_complete",
        before_state=before_state,
        changed_tasks=changed,
        correlation_id=correlation_id,
    )
    await session.commit()
    return {
        "operation": "ensure_reanalysis_complete",
        "changed_tasks": changed,
        "state": run.state,
        "message": "The current re-analysis will continue to a terminal outcome for every photo.",
    }


@router.post(
    "/agent/operations/reanalysis/pause",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_pause_reanalysis(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    from star.api.routers.reanalysis import _action

    run = await _current_agent_reanalysis(session)
    before_state = run.state
    changed = await _action(session, run, "pause")
    await _record_agent_reanalysis_control(
        session,
        run=run,
        operation="pause",
        before_state=before_state,
        changed_tasks=changed,
        correlation_id=correlation_id,
    )
    await session.commit()
    return {"operation": "pause_reanalysis", "changed_tasks": changed, "state": run.state}


@router.post(
    "/agent/operations/reanalysis/stop",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_stop_reanalysis(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Stop the current re-analysis gracefully; running work settles.

    A wave whose bookkeeping is beyond repair is ended here, and the scheduler
    starts a clean one on the current contract once the queue is quiet.
    """

    from star.api.routers.reanalysis import _action

    run = await _current_agent_reanalysis(session)
    before_state = run.state
    changed = await _action(session, run, "stop")
    await _record_agent_reanalysis_control(
        session,
        run=run,
        operation="stop",
        before_state=before_state,
        changed_tasks=changed,
        correlation_id=correlation_id,
    )
    await session.commit()
    return {"operation": "stop_reanalysis", "changed_tasks": changed, "state": run.state}


@router.post(
    "/agent/operations/reanalysis/dismiss-failures",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_dismiss_reanalysis_failures(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Accept the photographs the current re-analysis could never finish.

    Nothing is deleted: each becomes ``excluded`` with its evidence kept and the
    audit trail records what it was. Successful reviews are untouched, and a
    second call changes nothing.
    """

    from star.services.reanalysis_failures import dismiss_unresolved_failures

    run = await _current_agent_reanalysis(session)
    try:
        outcome = await dismiss_unresolved_failures(
            session,
            run=run,
            actor="star-agent-operations",
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return {"operation": "dismiss_reanalysis_failures", **outcome}


@router.post(
    "/agent/operations/history/prune",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_prune_history(
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Remove superseded generations and cancelled tasks older than three days.

    One bounded batch per call; call again until the counts come back zero.
    Nothing a photograph still points to, and nothing published, succeeded or
    failed, is touched.
    """

    from star.services.history_pruning import prune_history

    if session.get_bind().dialect.name == "postgresql":
        # Fit inside the web edge's window and never queue behind the audit tick
        # or another call: rows another pruner holds are simply skipped.
        await session.execute(text("SET LOCAL statement_timeout = 45000"))
        await session.execute(text("SET LOCAL lock_timeout = 2000"))
    outcome = await prune_history(session, generation_limit=1_000, task_limit=5_000)
    await session.commit()
    return {"operation": "prune_history", **outcome}


@router.post(
    "/agent/operations/reanalysis/resume",
    dependencies=[Depends(require_agent_operations)],
)
async def agent_resume_reanalysis(
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    from star.api.routers.reanalysis import _action

    run = await _current_agent_reanalysis(session)
    before_state = run.state
    changed = await _action(session, run, "resume")
    await _record_agent_reanalysis_control(
        session,
        run=run,
        operation="resume",
        before_state=before_state,
        changed_tasks=changed,
        correlation_id=correlation_id,
    )
    await session.commit()
    return {"operation": "resume_reanalysis", "changed_tasks": changed, "state": run.state}


def _sanitized_review_value(value: object) -> object:
    """Return bounded evidence while excluding media, identity, and transport data."""

    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower()
            safe_aggregate = normalized in {"face_count", "people_count", "person_count"}
            if not safe_aggregate and (
                normalized in REDACTED_REVIEW_DETAIL_KEYS
                or any(term in normalized for term in REDACTED_REVIEW_DETAIL_TERMS)
            ):
                continue
            sanitized[key] = _sanitized_review_value(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitized_review_value(item) for item in value[:200]]
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:512]


def _generation_review_value(generation: AnalysisGeneration) -> dict[str, object]:
    return {
        "sequence": generation.sequence,
        "scope": _sanitized_review_value(generation.scope),
        "start_stage": generation.start_stage,
        "expanded_stages": generation.expanded_stages,
        "reuse_policy": generation.reuse_policy,
        "publication_policy": generation.publication_policy,
        "application_release": generation.application_release,
        "personal_ranker_version_id": (
            str(generation.personal_ranker_version_id)
            if generation.personal_ranker_version_id is not None
            else None
        ),
        "personal_ranker_version": generation.personal_ranker_version,
        "model_policy_version": generation.model_policy_version,
        "inference_profiles": _sanitized_review_value(generation.inference_profiles),
        "prompt_version": generation.prompt_version,
        "prompt_revisions": _sanitized_review_value(generation.prompt_revisions),
        "schema_version": generation.schema_version,
        "calibration_version": generation.calibration_version,
        "state": generation.state,
        "reason": generation.reason,
        "published_at": generation.published_at,
        "completed_at": generation.completed_at,
        "failure_class": generation.failure_class,
        "created_at": generation.created_at,
        "updated_at": generation.updated_at,
    }


def _accepted_group_review_value(run: AnalysisRun) -> dict[str, object] | None:
    """Expose only the accepted per-photo Quick/Highlight allocation."""

    if run.analysis_type not in {"fast_group_rating", "highlight_comparison_allocation"}:
        return None
    payload = run.result_payload if isinstance(run.result_payload, dict) else {}
    allowed = {
        key: payload.get(key)
        for key in (
            "group_task_id",
            "candidate_index",
            "quality",
            "provisional_rating",
            "summary",
            "confidence",
            "group_duration_ms",
            "candidate_count",
            "allocated_compute_ms",
        )
        if key in payload
    }
    sanitized = _sanitized_review_value(allowed)
    if not isinstance(sanitized, dict):
        return None
    return {
        "prompt_version": run.processor_version.split(":", 1)[0],
        **sanitized,
    }


@router.get(
    "/agent/reviews",
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_reviews(
    asset_id: UUID | None = None,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return bounded current review evidence without media or source identifiers."""

    asset_query = select(Asset.id, Asset.current_generation_id).where(
        Asset.current_generation_id.is_not(None)
    )
    if asset_id is not None:
        asset_query = asset_query.where(Asset.id == asset_id)
    elif after is not None:
        asset_query = asset_query.where(Asset.id > after)
    asset_rows = list(
        (await session.execute(asset_query.order_by(Asset.id).limit(limit + 1))).all()
    )
    has_more = len(asset_rows) > limit
    asset_rows = asset_rows[:limit]
    generation_ids = [row.current_generation_id for row in asset_rows]
    if not generation_ids:
        return {
            "items": [],
            "count": 0,
            "next_after": None,
            "privacy": "No images, filenames, paths, OCR, identities, prompts, or credentials.",
        }

    generations = {
        item.id: item
        for item in (
            await session.scalars(
                select(AnalysisGeneration).where(AnalysisGeneration.id.in_(generation_ids))
            )
        ).all()
    }
    runs = list(
        (
            await session.scalars(
                select(AnalysisRun)
                .where(AnalysisRun.analysis_generation_id.in_(generation_ids))
                .order_by(AnalysisRun.started_at, AnalysisRun.id)
            )
        ).all()
    )
    run_ids = [run.id for run in runs]
    metrics = (
        list(
            (
                await session.scalars(
                    select(AssetMetric)
                    .where(AssetMetric.analysis_run_id.in_(run_ids))
                    .order_by(AssetMetric.name)
                )
            ).all()
        )
        if run_ids
        else []
    )
    apple_evidence = (
        list(
            (
                await session.scalars(
                    select(AppleVisionEvidence).where(
                        AppleVisionEvidence.analysis_run_id.in_(run_ids)
                    )
                )
            ).all()
        )
        if run_ids
        else []
    )
    proposals = list(
        (
            await session.scalars(
                select(Proposal)
                .where(
                    Proposal.analysis_generation_id.in_(generation_ids),
                    Proposal.proposal_type == ProposalType.RATING,
                    Proposal.state == ProposalState.PENDING,
                )
                .order_by(Proposal.created_at, Proposal.id)
            )
        ).all()
    )

    metrics_by_run: dict[UUID, list[dict[str, object]]] = {}
    for metric in metrics:
        metrics_by_run.setdefault(metric.analysis_run_id, []).append(
            {
                "name": metric.name,
                "value": metric.value,
                "uncertainty": metric.uncertainty,
                "explanation": metric.explanation[:2048],
                "details": _sanitized_review_value(metric.details),
            }
        )
    apple_by_run = {
        evidence.analysis_run_id: {
            "processor_version": evidence.processor_version,
            "aesthetics_score": evidence.aesthetics_score,
            "is_utility": evidence.is_utility,
            "classifications": _sanitized_review_value(evidence.classifications),
            "features": _sanitized_review_value(evidence.features),
            "worker_runtime_ms": evidence.worker_runtime_ms,
            "created_at": evidence.created_at,
        }
        for evidence in apple_evidence
    }
    runs_by_generation: dict[UUID, list[dict[str, object]]] = {}
    for run in runs:
        if run.analysis_generation_id is None:
            continue
        runs_by_generation.setdefault(run.analysis_generation_id, []).append(
            {
                "analysis_type": run.analysis_type,
                "processor_version": run.processor_version,
                "model_key": run.model_key,
                "status": run.status,
                "limitations": _sanitized_review_value(run.limitations),
                "error_class": run.error_class,
                "worker_runtime_ms": run.worker_runtime_ms,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "accepted_group_result": _accepted_group_review_value(run),
                "metrics": metrics_by_run.get(run.id, []),
                "apple_vision": apple_by_run.get(run.id),
            }
        )
    proposals_by_generation: dict[UUID, list[dict[str, object]]] = {}
    for proposal in proposals:
        if proposal.analysis_generation_id is None:
            continue
        sanitized_proposal = _sanitized_review_value(_proposal_value(proposal))
        if isinstance(sanitized_proposal, dict):
            sanitized_proposal.pop("id", None)
            proposals_by_generation.setdefault(proposal.analysis_generation_id, []).append(
                sanitized_proposal
            )

    items: list[dict[str, object]] = []
    for row in asset_rows:
        generation = generations.get(row.current_generation_id)
        if generation is None:
            continue
        items.append(
            {
                "asset_id": str(row.id),
                "generation": _generation_review_value(generation),
                "analysis_runs": runs_by_generation.get(generation.id, []),
                "rating_reviews": proposals_by_generation.get(generation.id, []),
            }
        )
    return {
        "items": items,
        "count": len(items),
        "next_after": str(asset_rows[-1].id) if has_more and asset_rows else None,
        "privacy": "No images, filenames, paths, OCR, identities, prompts, or credentials.",
    }


@router.get(
    "/agent/preferences",
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_preferences(
    asset_id: UUID | None = None,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return the owner's current rating labels through the scoped read-only key.

    The feed intentionally carries only the opaque Star asset identity and
    owner-authored preference evidence needed to join it with ``/agent/reviews``.
    It exposes no media, source identifiers, user identity, or write authority.
    """

    labels = sorted(
        await current_human_rating_labels(session),
        key=lambda item: item.asset_id,
    )
    if asset_id is not None:
        labels = [item for item in labels if item.asset_id == asset_id]
    elif after is not None:
        labels = [item for item in labels if item.asset_id > after]
    has_more = len(labels) > limit
    selected = labels[:limit]
    return {
        "items": [
            {
                "asset_id": str(item.asset_id),
                "owner_rating": item.rating,
                "reason": item.reason[:600] if item.reason is not None else None,
                "keywords": [str(value)[:80] for value in item.keywords[:24]],
                "created_at": item.created_at,
            }
            for item in selected
        ],
        "count": len(selected),
        "next_after": str(selected[-1].asset_id) if has_more and selected else None,
        "privacy": (
            "Owner-authored preference labels only. No images, filenames, source IDs, paths, "
            "OCR, identities, prompts, model responses, or credentials."
        ),
    }


async def _queued_advanced_by_currency(session: AsyncSession) -> dict[str, int]:
    """The queued Advanced reviews, grouped by whether and why a lease would refuse them.

    A queue of hundreds that leases nothing and a queue of hundreds that is
    about to be worked look identical as a count. This says which.
    """

    from star.services.task_scope import task_currency_reason

    queued = list(
        await session.scalars(
            select(Task)
            .where(Task.task_type == "asset.deep_review", Task.state == TaskState.QUEUED)
            .order_by(Task.available_at, Task.id)
            # Each row costs a few queries; a sample says as much as a census.
            .limit(60)
        )
    )
    histogram: dict[str, int] = {}
    for task in queued:
        reason = await task_currency_reason(session, task) or "current"
        histogram[reason] = histogram.get(reason, 0) + 1
    return histogram


def _stamp(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@router.get(
    "/agent/trace/{asset_id}",
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_photo_trace(
    asset_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Everything the coordinator holds about one photograph's journey, as states.

    "Why is this photo where it is?" answered in one call: each generation and
    its nodes, each task with its attempts and error class, each rating vote,
    the league entry, and the current projection. Only the caller's own asset
    id appears; every other identifier is left out, and the same redaction as
    the diagnostics applies to every detail.
    """

    from star.db.models import AssetCurationSnapshot, HighlightLeagueEntry

    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown photograph")
    generations = list(
        await session.scalars(
            select(AnalysisGeneration)
            .where(AnalysisGeneration.asset_id == asset_id)
            .order_by(AnalysisGeneration.sequence)
        )
    )
    generation_ids = [generation.id for generation in generations]
    nodes = (
        list(
            await session.scalars(
                select(AnalysisNode).where(AnalysisNode.generation_id.in_(generation_ids))
            )
        )
        if generation_ids
        else []
    )
    tasks = list(
        await session.scalars(
            select(Task).where(Task.asset_id == asset_id).order_by(Task.created_at)
        )
    )
    proposals = list(
        await session.scalars(
            select(Proposal)
            .where(Proposal.target_id == asset_id, Proposal.proposal_type == ProposalType.RATING)
            .order_by(Proposal.created_at)
        )
    )
    league = await session.scalar(
        select(HighlightLeagueEntry).where(HighlightLeagueEntry.asset_id == asset_id)
    )
    snapshot = await session.get(AssetCurationSnapshot, asset_id)
    nodes_by_generation: dict[UUID, list[AnalysisNode]] = {}
    for node in nodes:
        nodes_by_generation.setdefault(node.generation_id, []).append(node)
    tasks_by_generation: dict[UUID | None, list[Task]] = {}
    for task in tasks:
        tasks_by_generation.setdefault(task.analysis_generation_id, []).append(task)
    proposals_by_generation: dict[UUID | None, list[Proposal]] = {}
    for proposal in proposals:
        proposals_by_generation.setdefault(proposal.analysis_generation_id, []).append(proposal)

    def _task(task: Task) -> dict[str, object]:
        return {
            "task_type": task.task_type,
            "route": task.routing_key,
            "state": str(getattr(task.state, "value", task.state)),
            "attempt": task.attempt_count,
            "error_class": task.last_error_class,
            "origin": (task.target_ref or {}).get("queue_origin"),
            "created_at": _stamp(task.created_at),
            "updated_at": _stamp(task.updated_at),
        }

    def _proposal(proposal: Proposal) -> dict[str, object]:
        value = dict(proposal.value or {})
        return {
            "source": value.get("source"),
            "authority": proposal.authority_stage,
            "rating": value.get("rating"),
            "state": str(getattr(proposal.state, "value", proposal.state)),
            "reason_codes": list(proposal.reason_codes or [])[:8],
            "created_at": _stamp(proposal.created_at),
        }

    return {
        "photograph": str(asset_id),
        "access_state": asset.access_state,
        "current_generation_sequence": next(
            (g.sequence for g in generations if g.id == asset.current_generation_id), None
        ),
        "projection": {
            "effective_rating": snapshot.effective_rating if snapshot else None,
            "rating_source": snapshot.rating_source if snapshot else None,
            "analysis_complete": snapshot.analysis_complete if snapshot else None,
            "stage": snapshot.automated_rating_stage if snapshot else None,
        },
        "league": {
            "state": league.state,
            "five_star_state": league.five_star_state,
            "source_rating": league.source_rating,
            "matches": league.four_star_match_count,
            "conservative_score": league.conservative_score,
            "rank": league.five_star_rank,
        }
        if league
        else None,
        "generations": [
            {
                "sequence": generation.sequence,
                "state": generation.state,
                "reason": generation.reason,
                "failure_class": generation.failure_class,
                "prompt_version": generation.prompt_version,
                "release": generation.application_release,
                "created_at": _stamp(generation.created_at),
                "published_at": _stamp(generation.published_at),
                "superseded_at": _stamp(generation.superseded_at),
                "nodes": [
                    {
                        "stage": node.stage_key,
                        "state": node.state,
                        "required": node.required,
                        "applicable": node.applicable,
                        "reason": (node.decision or {}).get("reason_code"),
                        "completed_at": _stamp(node.completed_at),
                    }
                    for node in sorted(
                        nodes_by_generation.get(generation.id, []), key=lambda n: n.created_at
                    )
                ],
                "tasks": [_task(t) for t in tasks_by_generation.get(generation.id, [])],
                "votes": [_proposal(p) for p in proposals_by_generation.get(generation.id, [])],
            }
            for generation in generations
        ],
        "unattached_tasks": [_task(t) for t in tasks_by_generation.get(None, [])][:50],
        "privacy": "States, stages, reasons and timestamps only.",
    }


@router.get(
    "/agent/events/stream",
    dependencies=[Depends(require_agent_diagnostics)],
)
async def agent_event_stream(
    request: Request,
    after: int | None = None,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> StreamingResponse:
    """Sanitized operational events as they are recorded, for a live tail.

    Polls the durable event log by id, so nothing is missed across a reconnect:
    pass the last id seen as ``after``. Same redaction as the diagnostics.
    """

    await session.rollback()
    last_id = int(after) if after is not None else None

    async def generate() -> AsyncIterator[str]:
        nonlocal last_id
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                break
            async with reset_safe_session(session_factory) as tail:
                if last_id is None:
                    latest = await tail.scalar(select(func.max(OperationalEvent.id)))
                    last_id = int(latest or 0)
                rows = list(
                    await tail.scalars(
                        select(OperationalEvent)
                        .where(OperationalEvent.id > last_id)
                        .order_by(OperationalEvent.id)
                        .limit(200)
                    )
                )
            if not rows:
                yield f": keepalive {int(time.time())}\n\n"
                await asyncio.sleep(2.0)
                continue
            for event in rows:
                last_id = int(event.id)
                payload = {
                    "id": last_id,
                    "at": _stamp(event.created_at),
                    "severity": event.severity,
                    "source": event.source,
                    "event_type": event.event_type,
                    "message": event.message,
                    "details": _sanitized_event_detail(event.details),
                }
                yield f"id: {last_id}\nevent: star\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )
