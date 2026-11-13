from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.agent._base import ASSET_VLM_SCHEMA
from star.api.dependencies import Principal, require_roles
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    InferenceRun,
    OperationalEvent,
    Proposal,
    Task,
)
from star.db.session import get_session
from star.domain.enums import ProposalState, ProposalType, Role
from star.inference.policy import (
    APPLE_SYSTEM_PROMPT_KEY,
    NVIDIA_SYSTEM_PROMPT_KEY,
    RATING_SCALE_PROMPT_KEY,
)
from star.services.review_prompts import effective_prompt_text

router = APIRouter(prefix="/analysis-generations", tags=["Analysis audit"])
viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)

_SECRET_FIELD_NAMES = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "worker_credential",
}
_SAFE_TOKEN_COUNTERS = {"input_tokens", "output_tokens"}


def _redact_credentials(value: Any, *, field_name: str = "") -> Any:
    """Keep the private audit envelope useful without ever returning credentials."""

    normalized = field_name.casefold().replace("-", "_")
    if normalized in _SECRET_FIELD_NAMES or (
        normalized.endswith("_token") and normalized not in _SAFE_TOKEN_COUNTERS
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): _redact_credentials(item, field_name=str(key)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_credentials(item) for item in value]
    return value


def _generation_value(generation: AnalysisGeneration, *, current: bool) -> dict[str, Any]:
    return {
        "id": str(generation.id),
        "asset_id": str(generation.asset_id),
        "asset_revision_id": str(generation.asset_revision_id),
        "parent_generation_id": (
            str(generation.parent_generation_id) if generation.parent_generation_id else None
        ),
        "sequence": generation.sequence,
        "state": generation.state,
        "current": current,
        "reason": generation.reason,
        "scope": generation.scope,
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
        "inference_profiles": generation.inference_profiles,
        "prompt_version": generation.prompt_version,
        "prompt_revisions": generation.prompt_revisions,
        "schema_version": generation.schema_version,
        "calibration_version": generation.calibration_version,
        "created_at": generation.created_at,
        "completed_at": generation.completed_at,
        "published_at": generation.published_at,
        "superseded_at": generation.superseded_at,
        "failure_class": generation.failure_class,
    }


def _node_value(node: AnalysisNode) -> dict[str, Any]:
    return {
        "id": str(node.id),
        "stage": node.stage_key,
        "state": node.state,
        "required": node.required,
        "applicable": node.applicable,
        "analyzer_version": node.analyzer_version,
        "model_key": node.model_key,
        "inference_profile": node.inference_profile,
        "input_digest": node.input_digest,
        "attempt_count": node.attempt_count,
        "worker_runtime_ms": node.worker_runtime_ms,
        "failure_class": node.failure_class,
        "task_id": str(node.task_id) if node.task_id else None,
        "analysis_run_id": (
            str(node.result_analysis_run_id) if node.result_analysis_run_id else None
        ),
        "started_at": node.started_at,
        "completed_at": node.completed_at,
    }


def _proposal_value(proposal: Proposal) -> dict[str, Any]:
    value = proposal.value
    return {
        "id": str(proposal.id),
        "source": value.get("source"),
        "rating": value.get("rating"),
        "model_proposed_rating": value.get("model_proposed_rating", value.get("rating")),
        "rating_tier": value.get("rating_tier"),
        "semantic_score": value.get("semantic_score"),
        "adjusted_score": value.get("adjusted_score"),
        "hard_cap": value.get("hard_cap"),
        "hard_cap_reasons": value.get("hard_cap_reasons", []),
        "provisional_five_candidate": value.get("provisional_five_candidate", False),
        "rating_engine": value.get("rating_engine"),
        "star_probabilities": value.get("star_probabilities", {}),
        "description": value.get("description"),
        "observations": value.get("observations", value.get("observation_dossier")),
        "privacy_observation": value.get("privacy_observation"),
        "semantic_dimensions": value.get("semantic_dimensions"),
        "model_id": value.get("model_id"),
        "runtime": value.get("runtime"),
        "duration_ms": value.get("duration_ms"),
        "created_at": proposal.created_at,
    }


async def _generation_payload(
    session: AsyncSession,
    generation: AnalysisGeneration,
) -> dict[str, Any]:
    asset = await session.get(Asset, generation.asset_id)
    nodes = list(
        await session.scalars(
            select(AnalysisNode)
            .where(AnalysisNode.generation_id == generation.id)
            .order_by(AnalysisNode.created_at, AnalysisNode.stage_key)
        )
    )
    proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.analysis_generation_id == generation.id,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
            )
            .order_by(Proposal.created_at, Proposal.id)
        )
    )
    return {
        **_generation_value(
            generation,
            current=bool(asset and asset.current_generation_id == generation.id),
        ),
        "nodes": [_node_value(node) for node in nodes],
        "proposals": [_proposal_value(proposal) for proposal in proposals],
    }


def _task_value(task: Task) -> dict[str, Any]:
    """Return the durable coordinator envelope without worker credentials or media bytes."""

    return {
        "id": str(task.id),
        "task_type": task.task_type,
        "routing_key": task.routing_key,
        "data_class": task.data_class,
        "completion_role": task.completion_role,
        "state": task.state.value,
        "priority": task.priority,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "target_ref": _redact_credentials(task.target_ref),
        "requirements": _redact_credentials(task.requirements),
        "progress": _redact_credentials(task.progress),
        "checkpoint": _redact_credentials(task.checkpoint),
        "last_error_class": task.last_error_class,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _analysis_run_value(run: AnalysisRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "task_id": None,
        "analysis_type": run.analysis_type,
        "processor_version": run.processor_version,
        "model_key": run.model_key,
        "status": run.status,
        "limitations": run.limitations,
        "input_provenance": run.input_provenance,
        "accepted_result_json": run.result_payload,
        "result_digest": run.result_digest,
        "error_class": run.error_class,
        "worker_runtime_ms": run.worker_runtime_ms,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _inference_run_value(run: InferenceRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "task_id": str(run.task_id) if run.task_id else None,
        "analysis_run_id": str(run.analysis_run_id) if run.analysis_run_id else None,
        "invocation_index": run.invocation_index,
        "phase": run.phase,
        "model_key": run.model_key,
        "model_reported_version": run.model_reported_version,
        "runtime": run.runtime,
        "prompt_version": run.prompt_version,
        "schema_version": run.schema_version,
        "input_digest": run.input_digest,
        "output_digest": run.output_digest,
        "image_count": run.image_count,
        "request_count": run.request_count,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "duration_ms": run.duration_ms,
        "state": run.state,
        "error_class": run.error_class,
        "completed_at": run.completed_at,
    }


def _event_value(event: OperationalEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "severity": event.severity,
        "source": event.source,
        "event_type": event.event_type,
        "message": event.message,
        "correlation_id": event.correlation_id,
        "task_id": str(event.task_id) if event.task_id else None,
        "worker_id": str(event.worker_id) if event.worker_id else None,
        "details": _redact_credentials(event.details),
        "created_at": event.created_at,
    }


async def _prompt_contract(session: AsyncSession, task: Task) -> dict[str, Any] | None:
    """Expose the effective bounded prompt contract used by current workers.

    Older runs did not persist the provider request body. The durable task envelope and
    invocation digests remain exact; prompt text is therefore clearly marked as reconstructed
    from the currently effective version instead of being misrepresented as a historical byte
    snapshot.
    """

    target = task.target_ref
    task_type = task.task_type
    if task_type not in {"asset.primary_review", "asset.deep_review"}:
        return None
    rating_scale = await effective_prompt_text(session, RATING_SCALE_PROMPT_KEY)
    if task_type == "asset.primary_review":
        prompt_key = "primary_review"
        system_key = NVIDIA_SYSTEM_PROMPT_KEY
        phase = "primary_visual"
    else:
        prompt_key = f"photo_deep_review:{target.get('prompt_version') or 'senior-review-v1'}"
        system_key = APPLE_SYSTEM_PROMPT_KEY
        phase = "senior_review"
    phases: list[dict[str, Any]] = [
        {
            "phase": phase,
            "task": prompt_key,
            "instruction": await effective_prompt_text(session, prompt_key),
            "json_schema": ASSET_VLM_SCHEMA,
        }
    ]
    return {
        "task_id": str(task.id),
        "task_type": task.task_type,
        "capture_state": "reconstructed_current_effective_prompt",
        "historical_byte_exact": False,
        "prompt_key": prompt_key,
        "system_prompt_key": system_key,
        "system_prompt": await effective_prompt_text(session, system_key),
        "rating_scale": rating_scale,
        "phases": phases,
        "durable_task_target": _redact_credentials(target),
    }


@router.get("")
async def list_analysis_generations(
    limit: int = Query(default=100, ge=1, le=500),
    state: str | None = Query(default=None, max_length=32),
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    statement = select(AnalysisGeneration, Asset.current_generation_id).join(
        Asset, Asset.id == AnalysisGeneration.asset_id
    )
    if state:
        statement = statement.where(AnalysisGeneration.state == state)
    rows = list(
        await session.execute(statement.order_by(AnalysisGeneration.created_at.desc()).limit(limit))
    )
    return [
        _generation_value(generation, current=current_id == generation.id)
        for generation, current_id in rows
    ]


@router.get("/assets/{asset_id}")
async def list_asset_analysis_generations(
    asset_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    generations = list(
        await session.scalars(
            select(AnalysisGeneration)
            .where(AnalysisGeneration.asset_id == asset_id)
            .order_by(AnalysisGeneration.sequence.desc())
        )
    )
    return [
        _generation_value(value, current=asset.current_generation_id == value.id)
        for value in generations
    ]


@router.get("/by-id/{generation_id}")
async def get_analysis_generation(
    generation_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    generation = await session.get(AnalysisGeneration, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="analysis generation not found")
    return await _generation_payload(session, generation)


@router.get("/by-id/{generation_id}/evidence")
async def get_analysis_generation_evidence(
    generation_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a private, bounded trace of one photo-analysis generation.

    Accepted structured results may contain private visual descriptions and therefore remain in
    this authenticated interface. They never reach the operational event log.
    """

    generation = await session.get(AnalysisGeneration, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="analysis generation not found")
    tasks = list(
        await session.scalars(
            select(Task)
            .where(Task.analysis_generation_id == generation.id)
            .order_by(Task.created_at, Task.id)
        )
    )
    runs = list(
        await session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.analysis_generation_id == generation.id)
            .order_by(AnalysisRun.started_at, AnalysisRun.id)
        )
    )
    inference_runs = list(
        await session.scalars(
            select(InferenceRun)
            .where(InferenceRun.analysis_generation_id == generation.id)
            .order_by(InferenceRun.completed_at, InferenceRun.invocation_index)
        )
    )
    task_ids = [task.id for task in tasks]
    events = (
        list(
            await session.scalars(
                select(OperationalEvent)
                .where(OperationalEvent.task_id.in_(task_ids))
                .order_by(OperationalEvent.created_at, OperationalEvent.id)
                .limit(500)
            )
        )
        if task_ids
        else []
    )
    prompt_contracts = [
        contract
        for task in tasks
        if (contract := await _prompt_contract(session, task)) is not None
    ]
    return {
        "generation_id": str(generation.id),
        "asset_id": str(generation.asset_id),
        "privacy": {
            "scope": "authenticated_star_only",
            "exported_to_metrics": False,
            "exported_to_logs": False,
            "raw_provider_text_retained": False,
            "note": (
                "Accepted schema-valid JSON is retained. Raw provider streams, hidden reasoning, "
                "media bytes, and invalid pre-correction output are not retained."
            ),
        },
        "prompt_contracts": prompt_contracts,
        "tasks": [_task_value(task) for task in tasks],
        "analysis_runs": [_analysis_run_value(run) for run in runs],
        "inference_runs": [_inference_run_value(run) for run in inference_runs],
        "events": [_event_value(event) for event in events],
    }
