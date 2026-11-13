from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.asset_vlm_submission import (
    record_asset_vlm_submission,
)
from star.analysis.review_pipeline import (
    record_qwen_completion,
)
from star.analysis.semantic_embedding_submission import (
    record_semantic_embedding_submission,
)
from star.api.dependencies import request_id
from star.api.routers.workers._shared import (
    _authenticated_worker,
    _clear_lease,
    _leased_task,
)
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import (
    HighlightComparison,
    LibraryReviewNote,
    Task,
)
from star.db.session import get_session
from star.domain.enums import TaskState
from star.domain.schemas import (
    AssetVlmResultSubmission,
    HighlightComparisonResultSubmission,
    PhotoObservationResultSubmission,
    SemanticEmbeddingResultSubmission,
    StarRatingResultSubmission,
    WorkerTaskFailure,
)
from star.domain.schemas.library_review_schemas import LibraryReviewSubmission
from star.services.analysis_quarantine import quarantine_repeated_primary_failure
from star.services.audit import AuditRecord, append_audit_event
from star.services.evidence_persistence import record_task_failure_evidence
from star.services.execution_epoch import cancel_generation_less_deep_review
from star.services.global_ranking import refresh_global_ranking_if_task_type_idle
from star.services.highlight_league import record_highlight_comparison
from star.services.library_review import record_review_result
from star.services.operations import OperationRecord, append_operation_event
from star.services.similarity_series import record_series_selection
from star.services.source_media import record_permanent_source_failure
from star.services.task_recovery import (
    RECOVERABLE_BUDGET_TASK_TYPES,
    RECOVERABLE_MODEL_TASK_TYPES,
    REPLAYABLE_STRUCTURED_OUTPUT_ERRORS,
    TRANSIENT_RUNTIME_ERROR_CLASSES,
    advance_structured_output_seed,
    apply_runtime_fallback_envelope,
    release_rejected_result,
    return_after_slow_inference_timeout,
    return_after_terminal_structured_output_failure,
    return_after_transient_runtime_failure,
)


async def _verify_execution_identity(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    """A result must prove the same immutable instructions as its lease."""
    if "/complete" not in request.url.path:
        return
    await _authenticated_worker(request.headers.get("authorization"), session, settings)
    raw_id = request.path_params.get("task_id")
    try:
        task_id = uuid.UUID(str(raw_id))
    except ValueError:
        return  # The route's normal validation supplies the 422 response.
    task = await session.get(Task, task_id)
    expected = task.target_ref.get("execution_manifest_digest") if task else None
    if expected and request.headers.get("x-star-execution-manifest") != expected:
        raise HTTPException(status_code=409, detail="execution manifest identity mismatch")


async def _release_rejected(session: AsyncSession, task: Task, detail: str) -> None:
    """Roll the refused submission back and free the lease in its own transaction."""

    await session.rollback()
    fresh = await session.get(Task, task.id)
    if fresh is None:
        return
    release_rejected_result(fresh, now=utc_now(), detail=detail)
    await session.commit()


router = APIRouter(
    prefix="/workers", tags=["Workers"], dependencies=[Depends(_verify_execution_identity)]
)


@router.post(
    "/tasks/{task_id}/complete-semantic-embedding",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def complete_semantic_embedding_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: SemanticEmbeddingResultSubmission,
    authorization: str | None = Header(default=None),
    result_digest: str | None = Header(default=None, alias="X-Star-Result-Digest"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        result_digest=result_digest,
    )
    try:
        run = await record_semantic_embedding_submission(session, task=task, payload=payload)
    except ValueError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="semantic_embedding.completed",
            message="Local semantic relationship evidence completed",
            task_id=task.id,
            worker_id=auth.worker.id,
            connection_id=task.connection_id,
            details={
                "analysis_run_id": str(run.id),
                "algorithm_version": payload.processor_version,
                "dimensions": len(payload.embedding),
            },
        ),
    )
    await session.commit()


@router.post(
    "/tasks/{task_id}/complete-highlight-comparison",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def complete_highlight_comparison_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: HighlightComparisonResultSubmission,
    authorization: str | None = Header(default=None),
    result_digest: str | None = Header(default=None, alias="X-Star-Result-Digest"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        result_digest=result_digest,
    )
    try:
        await record_highlight_comparison(session, task=task, payload=payload)
    except ValueError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_operation_event(
        session,
        OperationRecord(
            source="model",
            event_type="model.highlight_comparison.completed",
            message="Collection-relative highlight comparison completed",
            task_id=task.id,
            worker_id=auth.worker.id,
            connection_id=task.connection_id,
            details={"task_type": task.task_type, "state": task.state.value},
        ),
    )
    await refresh_global_ranking_if_task_type_idle(
        session,
        task_type="group.highlight_compare",
        reason="highlight_block_complete",
    )
    await session.commit()


@router.post(
    "/tasks/{task_id}/complete-series-selection",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def complete_series_selection_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: HighlightComparisonResultSubmission,
    authorization: str | None = Header(default=None),
    result_digest: str | None = Header(default=None, alias="X-Star-Result-Digest"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> None:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        result_digest=result_digest,
    )
    try:
        group = await record_series_selection(session, task=task, payload=payload)
    except ValueError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="model.series_selection.completed",
            actor_type="worker",
            actor_id=str(auth.worker.id),
            target_ref={"task_id": str(task.id), "group_id": str(group.id)},
            correlation_id=correlation_id,
            after={
                "representative_asset_id": str(group.representative_asset_id),
                "runtime": payload.runtime,
                "duration_ms": payload.duration_ms,
            },
        ),
    )
    await session.commit()


@router.post("/tasks/{task_id}/complete-library-review", status_code=status.HTTP_204_NO_CONTENT)
async def complete_library_review_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: LibraryReviewSubmission,
    authorization: str | None = Header(default=None),
    result_digest: str | None = Header(default=None, alias="X-Star-Result-Digest"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> None:
    """Retain one nightly reading of the library.

    Findings and proposals are stored as written. Nothing here changes a rating,
    a prompt or a setting: a proposal is a suggestion until the owner accepts it,
    because model prose must never become an instruction on its own.
    """

    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        result_digest=result_digest,
        allow_cancel_requested=True,
    )
    note = await session.get(LibraryReviewNote, payload.note_id)
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="library review note no longer exists",
        )
    record_review_result(note, payload.value, model_key=payload.model_key)
    task.state = TaskState.SUCCEEDED
    _clear_lease(task)
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="library.review.recorded",
            message="The library reviewed itself and proposed next steps",
            task_id=task.id,
            worker_id=auth.worker.id,
            correlation_id=correlation_id,
            details={
                "note_id": str(note.id),
                "findings": len(note.findings),
                "proposals": [str(item.get("action")) for item in note.proposals],
            },
        ),
    )
    await session.commit()


@router.post("/tasks/{task_id}/complete-asset-vlm", status_code=status.HTTP_204_NO_CONTENT)
async def complete_asset_vlm_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: (
        StarRatingResultSubmission | AssetVlmResultSubmission | PhotoObservationResultSubmission
    ),
    authorization: str | None = Header(default=None),
    result_digest: str | None = Header(default=None, alias="X-Star-Result-Digest"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> None:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        result_digest=result_digest,
    )
    if not isinstance(
        payload,
        (StarRatingResultSubmission, AssetVlmResultSubmission, PhotoObservationResultSubmission),
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this task requires the star rating schema",
        )
    if task.task_type == "asset.deep_review" and task.analysis_generation_id is None:
        # The generation was pruned while the review ran; the answer can
        # publish nothing. Settle the task so the lane and the lease are
        # free at once instead of answering 409 and keeping the lease.
        cancel_generation_less_deep_review(task, now=utc_now())
        await session.commit()
        return
    try:
        run, proposal = await record_asset_vlm_submission(session, task=task, payload=payload)
        run.input_provenance = {
            **run.input_provenance,
            "execution_manifest_digest": task.target_ref.get("execution_manifest_digest"),
            "owner_calibration": task.target_ref.get("owner_calibration"),
        }
    except ValueError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        await _release_rejected(session, task, str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    proposed_rating = int(proposal.value.get("rating", 0)) if proposal is not None else 0
    if task.task_type == "asset.deep_review" and 1 <= proposed_rating <= 5:
        assert task.analysis_generation_id is not None
        await record_qwen_completion(
            session,
            asset_id=run.asset_id,
            analysis_generation_id=task.analysis_generation_id,
            rating=proposed_rating,
        )
    await append_audit_event(
        session,
        AuditRecord(
            event_type=(
                "model.deep_review.completed"
                if task.task_type == "asset.deep_review"
                else "model.primary_review.completed"
            ),
            actor_type="worker",
            actor_id=str(auth.worker.id),
            target_ref={
                "task_id": str(task.id),
                "analysis_run_id": str(run.id),
                "proposal_id": str(proposal.id) if proposal is not None else None,
                "asset_id": str(run.asset_id),
            },
            correlation_id=correlation_id,
            after={
                "runtime": payload.runtime,
                "duration_ms": payload.duration_ms,
                "proposed_rating": proposed_rating,
                "proposal_only": True,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="model",
            event_type=(
                "model.deep_review.completed"
                if task.task_type == "asset.deep_review"
                else "model.primary_review.completed"
            ),
            message=(
                "Senior photo review completed"
                if task.task_type == "asset.deep_review"
                else "Primary photo review completed"
            ),
            task_id=task.id,
            worker_id=auth.worker.id,
            model_id=run.model_version_id,
            connection_id=task.connection_id,
            details={
                "runtime": payload.runtime,
                "duration_ms": payload.duration_ms,
                "state": task.state.value,
            },
        ),
    )
    await session.commit()


@router.post("/tasks/{task_id}/fail", status_code=status.HTTP_204_NO_CONTENT)
async def fail_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: WorkerTaskFailure,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        allow_cancel_requested=True,
    )
    task.last_error_class = payload.error_class
    failed_attempt_number = task.attempt_count
    cancel_requested = (
        task.state == TaskState.CANCEL_REQUESTED or payload.error_class == "TaskCancelled"
    )
    failure_evidence = await record_task_failure_evidence(
        session,
        task=task,
        attempt_number=failed_attempt_number,
        worker_id=auth.worker.id,
        error_class=payload.error_class,
        retryable=payload.retryable,
        reason=payload.reason,
        input_digest=payload.input_digest,
        output_digest=payload.output_digest,
        duration_ms=payload.duration_ms,
        prompt_version=payload.prompt_version,
        schema_version=payload.schema_version,
    )
    retry_delay_seconds: int | None = None
    if cancel_requested:
        task.state = TaskState.CANCELLED
    elif (
        payload.retryable
        and payload.error_class == "PhotoComputeBudgetExceeded"
        and task.task_type in RECOVERABLE_BUDGET_TASK_TYPES
    ):
        retry_delay_seconds = return_after_slow_inference_timeout(task)
    elif (
        payload.retryable
        and task.task_type in RECOVERABLE_MODEL_TASK_TYPES
        and payload.error_class in TRANSIENT_RUNTIME_ERROR_CLASSES
    ):
        if payload.error_class == "LocalModelOutOfMemoryError":
            # An unload plus a byte-identical replay reproduces the same OOM;
            # the declared fallback envelope gives the retry a real chance.
            apply_runtime_fallback_envelope(task)
        retry_delay_seconds = return_after_transient_runtime_failure(task)
    elif (
        payload.retryable
        and task.task_type in RECOVERABLE_MODEL_TASK_TYPES
        and payload.error_class in REPLAYABLE_STRUCTURED_OUTPUT_ERRORS
        and task.attempt_count >= task.max_attempts
    ):
        retry_delay_seconds = (
            None
            if task.task_type == "asset.primary_review"
            else return_after_terminal_structured_output_failure(task)
        )
        if retry_delay_seconds is None:
            task.state = TaskState.FAILED
        else:
            advance_structured_output_seed(task)
    elif payload.retryable and task.attempt_count < task.max_attempts:
        task.state = TaskState.RETRY_WAITING
        if payload.error_class in REPLAYABLE_STRUCTURED_OUTPUT_ERRORS:
            advance_structured_output_seed(task)
        retry_delay_seconds = (
            300
            if task.task_type == "asset.primary_review"
            and payload.error_class in REPLAYABLE_STRUCTURED_OUTPUT_ERRORS
            else min(300, 2**task.attempt_count)
        )
        task.available_at = utc_now() + timedelta(seconds=retry_delay_seconds)
    elif (
        payload.error_class == "LocalInferenceContextWindowError"
        and task.task_type in RECOVERABLE_MODEL_TASK_TYPES
        and apply_runtime_fallback_envelope(task)
    ):
        # The worker classifies a context overflow as deterministic and
        # non-retryable; it becomes retryable exactly once by switching to the
        # contract's declared fallback envelope.
        retry_delay_seconds = return_after_transient_runtime_failure(task)
    else:
        task.state = TaskState.FAILED
    if task.task_type == "group.highlight_compare" and payload.source_asset_id is not None:
        # A comparison round that could not load one of its photographs can
        # never succeed with the same photograph in it, whatever Immich's
        # reason was. Counting the failure here is what retires the entry
        # after two tries; retrying the round only spoils it again, so the
        # round leaves the queue now instead of waiting to fail twice more.
        from star.services.highlight_league import mark_highlight_source_unavailable

        await mark_highlight_source_unavailable(session, payload.source_asset_id)
        task.state = TaskState.CANCELLED
        task.cancel_requested_at = utc_now()
        task.last_error_class = "SourceMediaUnavailableError"
    source_outcome = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=payload.source_asset_id,
        source_error_code=payload.source_error_code,
    )
    if source_outcome.quarantined:
        if source_outcome.asset is None:  # pragma: no cover - service invariant
            raise RuntimeError("quarantined source failure has no asset")
        task.state = TaskState.CANCELLED
        task.last_error_class = "SourceMediaQuarantined"
        await append_audit_event(
            session,
            AuditRecord(
                event_type="asset.source_media.quarantined",
                actor_type="worker",
                actor_id=str(auth.worker.id),
                target_ref={"asset_id": str(source_outcome.asset.id)},
                correlation_id=f"worker-failure:{failure_evidence.id}",
                after={
                    "access_state": "quarantined",
                    "source_failure_code": payload.source_error_code,
                    "source_failure_count": source_outcome.asset.source_failure_count,
                    "cancelled_tasks": source_outcome.cancelled_tasks,
                    "immich_unchanged": True,
                },
            ),
        )
    analysis_outcome = await quarantine_repeated_primary_failure(
        session,
        task=task,
        error_class=payload.error_class,
    )
    if analysis_outcome.quarantined:
        if analysis_outcome.asset is None:  # pragma: no cover - service invariant
            raise RuntimeError("analysis quarantine has no asset")
        await append_audit_event(
            session,
            AuditRecord(
                event_type="asset.analysis.quarantined",
                actor_type="worker",
                actor_id=str(auth.worker.id),
                target_ref={"asset_id": str(analysis_outcome.asset.id)},
                correlation_id=f"worker-failure:{failure_evidence.id}",
                after={
                    "access_state": "quarantined",
                    "analysis_failure_code": payload.error_class,
                    "analysis_failure_count": analysis_outcome.asset.analysis_failure_count,
                    "cancelled_tasks": analysis_outcome.cancelled_tasks,
                    "immich_unchanged": True,
                },
            ),
        )
        await append_operation_event(
            session,
            OperationRecord(
                source="worker",
                event_type="asset.analysis.quarantined",
                message="Photo was excluded after two photo-specific Primary failures",
                severity="warning",
                task_id=task.id,
                worker_id=auth.worker.id,
                connection_id=task.connection_id,
                details={
                    "error_class": payload.error_class,
                    "attempts": analysis_outcome.asset.analysis_failure_count,
                    "cancelled_tasks": analysis_outcome.cancelled_tasks,
                    "immich_unchanged": True,
                },
            ),
        )
    final_state = task.state.value
    task.progress = {
        **dict(task.progress or {}),
        "source_error_code": payload.source_error_code,
    }
    await _settle_failed_highlight_comparison(session, task, final_state)
    _clear_lease(task)
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="task.failed" if final_state == TaskState.FAILED.value else "task.returned",
            message="Worker reported a task failure"
            if final_state == TaskState.FAILED.value
            else "Worker returned an unfinished task",
            severity=(
                "info"
                if source_outcome.asset is not None
                else "error"
                if final_state == TaskState.FAILED.value
                else "warning"
            ),
            task_id=task.id,
            worker_id=auth.worker.id,
            connection_id=task.connection_id,
            details={
                "task_type": task.task_type,
                "error_class": payload.error_class,
                "retryable": payload.retryable,
                "state": final_state,
                "attempt": failed_attempt_number,
                "retry_delay_seconds": retry_delay_seconds,
                "runtime_failure_count": (task.progress or {}).get("runtime_failure_count"),
                "runtime_failure_limit": (task.progress or {}).get("runtime_failure_limit"),
                "failure_evidence_id": str(failure_evidence.id),
                "reason_digest": failure_evidence.reason_digest,
            },
        ),
    )
    await session.commit()


async def _settle_failed_highlight_comparison(
    session: AsyncSession,
    task: Task,
    final_state: str,
) -> None:
    if task.task_type != "group.highlight_compare":
        return
    try:
        comparison_id = uuid.UUID(str(task.target_ref.get("comparison_id")))
    except TypeError, ValueError:
        return
    comparison = await session.get(HighlightComparison, comparison_id)
    if comparison is None or comparison.task_id != task.id:
        return
    if final_state == TaskState.RETRY_WAITING.value:
        comparison.state = "queued"
        comparison.completed_at = None
        comparison.explanation = None
        return
    if final_state not in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
        return
    comparison.state = final_state.lower()
    comparison.completed_at = utc_now()
    comparison.explanation = "The local comparison did not complete."
