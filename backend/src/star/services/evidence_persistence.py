from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.db.models import (
    AnalysisGeneration,
    Asset,
    HighlightComparison,
    ModelVersion,
    Task,
    TaskFailureEvidence,
    Worker,
)
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.campaigns import canonical_digest

EVIDENCE_PROVENANCE_CONTRACT = "accepted-evidence-v1"

_INFERENCE_PROFILE_KEYS = (
    "context_tokens",
    "max_completion_tokens",
    "visual_token_budget",
    "max_media_edge",
    "contact_sheet_edge",
    "compute_budget_ms",
    "reasoning_effort",
    "runtime_contract_revision",
    "seed",
    "temperature",
    "top_p",
    "top_k",
)
_WORKER_RELEASE_KEYS = (
    "software_version",
    "agent_package_version",
    "worker_protocol_version",
    "build_git_sha",
    "build_tree_state",
    "build_source_fingerprint",
)


def server_release_provenance() -> dict[str, str]:
    return {
        "software_version": __version__,
        "image_tag": os.environ.get("STAR_IMAGE_TAG", "unknown"),
        "build_git_sha": os.environ.get("STAR_BUILD_GIT_SHA", "unknown"),
        "build_tree_state": os.environ.get("STAR_BUILD_TREE_STATE", "unknown"),
        "build_source_fingerprint": os.environ.get("STAR_BUILD_SOURCE_FINGERPRINT", "unknown"),
    }


def _task_inference_profile(task: Task) -> dict[str, Any]:
    profile = {
        key: task.target_ref[key] for key in _INFERENCE_PROFILE_KEYS if key in task.target_ref
    }
    profile["routing_key"] = task.routing_key
    profile["estimated_work_class"] = task.estimated_work_class
    return profile


def _target_uuid(task: Task, key: str) -> str | None:
    value = task.target_ref.get(key)
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _target_candidate_ids(task: Task) -> list[uuid.UUID]:
    values = task.target_ref.get("asset_ids", [])
    if not isinstance(values, list):
        return []
    candidate_ids: list[uuid.UUID] = []
    for value in values:
        try:
            candidate_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return candidate_ids


async def task_evidence_provenance(
    session: AsyncSession,
    *,
    task: Task,
    model_key: str | None,
    prompt_version: str | None,
    schema_version: str | None,
    runtime: str | None,
    candidate_asset_ids: Iterable[uuid.UUID] = (),
) -> dict[str, Any]:
    """Build a bounded provenance snapshot without prompts or private outputs."""

    generation = (
        await session.get(AnalysisGeneration, task.analysis_generation_id)
        if task.analysis_generation_id is not None
        else None
    )
    worker = (
        await session.get(Worker, task.leased_to_worker_id)
        if task.leased_to_worker_id is not None
        else None
    )
    requested_candidate_ids = list(candidate_asset_ids)
    candidate_ids = list(dict.fromkeys(requested_candidate_ids or _target_candidate_ids(task)))
    candidate_assets = (
        list(await session.scalars(select(Asset).where(Asset.id.in_(candidate_ids))))
        if candidate_ids
        else []
    )
    revision_by_asset = {
        str(asset.id): str(asset.current_revision_id) if asset.current_revision_id else None
        for asset in candidate_assets
    }
    application_release = (
        generation.application_release
        if generation is not None
        else str(task.target_ref.get("candidate_release") or f"v{__version__.split('.', 1)[0]}")
    )
    worker_release: dict[str, Any] = {}
    if worker is not None:
        worker_release = {
            "worker_id": str(worker.id),
            "hostname_label": worker.hostname_label,
            **{
                key: worker.runtime_status[key]
                for key in _WORKER_RELEASE_KEYS
                if key in worker.runtime_status
            },
        }
        worker_release.setdefault("software_version", worker.software_version)
    return {
        "contract": EVIDENCE_PROVENANCE_CONTRACT,
        "application_release": application_release,
        "server_release": server_release_provenance(),
        "task": {
            "id": str(task.id),
            "type": task.task_type,
            "attempt": task.attempt_count,
            "processing_batch_id": (
                str(task.processing_batch_id) if task.processing_batch_id else None
            ),
            "campaign_id": str(task.campaign_id) if task.campaign_id else None,
        },
        "asset": {
            "id": str(task.asset_id) if task.asset_id else _target_uuid(task, "asset_id"),
            "revision_id": (
                str(task.asset_revision_id)
                if task.asset_revision_id
                else _target_uuid(task, "asset_revision_id")
            ),
            "analysis_generation_id": (
                str(task.analysis_generation_id) if task.analysis_generation_id else None
            ),
        },
        "candidates": [
            {"asset_id": str(asset_id), "asset_revision_id": revision_by_asset.get(str(asset_id))}
            for asset_id in candidate_ids
        ],
        "model": {
            "key": model_key,
            "version_id": task.target_ref.get("model_version_id"),
            "reported_runtime": runtime,
        },
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "inference_profile": _task_inference_profile(task),
        "worker_release": worker_release,
    }


async def record_task_failure_evidence(
    session: AsyncSession,
    *,
    task: Task,
    attempt_number: int,
    worker_id: uuid.UUID | None,
    error_class: str,
    retryable: bool,
    reason: str | None,
    input_digest: str | None,
    output_digest: str | None,
    duration_ms: int | None,
    prompt_version: str | None,
    schema_version: str | None,
) -> TaskFailureEvidence:
    """Persist an idempotent failed-attempt envelope without private text."""

    model_key_value = task.target_ref.get("model_key")
    model_key = str(model_key_value) if model_key_value is not None else None
    effective_prompt = prompt_version or _optional_string(task.target_ref.get("prompt_version"))
    effective_schema = schema_version or _optional_string(task.target_ref.get("schema_version"))
    provenance = await task_evidence_provenance(
        session,
        task=task,
        model_key=model_key,
        prompt_version=effective_prompt,
        schema_version=effective_schema,
        runtime=None,
    )
    existing = await session.scalar(
        select(TaskFailureEvidence).where(
            TaskFailureEvidence.task_id == task.id,
            TaskFailureEvidence.attempt_number == attempt_number,
            TaskFailureEvidence.error_class == error_class,
        )
    )
    reason_digest = canonical_digest(reason) if reason else None
    if existing is not None:
        expected = (
            existing.retryable,
            existing.reason_digest,
            existing.input_digest,
            existing.output_digest,
            existing.duration_ms,
        )
        received = (retryable, reason_digest, input_digest, output_digest, duration_ms)
        if expected != received:
            raise RuntimeError("failed-attempt evidence is immutable")
        return existing
    model_version_value = task.target_ref.get("model_version_id")
    try:
        candidate_model_version_id = (
            uuid.UUID(str(model_version_value)) if model_version_value else None
        )
    except ValueError:
        candidate_model_version_id = None
    model_version_id = (
        candidate_model_version_id
        if candidate_model_version_id is not None
        and await session.get(ModelVersion, candidate_model_version_id) is not None
        else None
    )
    evidence = TaskFailureEvidence(
        task_id=task.id,
        analysis_generation_id=task.analysis_generation_id,
        asset_id=task.asset_id,
        asset_revision_id=task.asset_revision_id,
        worker_id=worker_id,
        model_version_id=model_version_id,
        task_type=task.task_type,
        attempt_number=attempt_number,
        error_class=error_class,
        retryable=retryable,
        reason_digest=reason_digest,
        input_digest=input_digest,
        output_digest=output_digest,
        duration_ms=duration_ms,
        model_key=model_key,
        prompt_version=effective_prompt,
        schema_version=effective_schema,
        inference_profile=dict(provenance["inference_profile"]),
        application_release=str(provenance["application_release"]),
        release_provenance={
            "contract": provenance["contract"],
            "server_release": provenance["server_release"],
            "worker_release": provenance["worker_release"],
            "task": provenance["task"],
            "asset": provenance["asset"],
            "candidates": provenance["candidates"],
            "model": provenance["model"],
        },
    )
    session.add(evidence)
    await session.flush()
    return evidence


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


async def seal_comparison_evidence(
    session: AsyncSession,
    *,
    task: Task,
    comparison: HighlightComparison,
    payload: HighlightComparisonResultSubmission,
    candidate_asset_ids: Iterable[uuid.UUID],
) -> None:
    """Attach non-duplicating provenance to a canonical comparison payload."""

    result_document = payload.model_dump(mode="json")
    digest = canonical_digest(result_document)
    if comparison.result_digest is not None and comparison.result_digest != digest:
        raise RuntimeError("completed comparison evidence is immutable")
    schema_version = (
        payload.inference_runs[-1].schema_version
        if payload.inference_runs
        else "highlight-comparison-result-v1"
    )
    comparison.result_digest = digest
    comparison.evidence_provenance = await task_evidence_provenance(
        session,
        task=task,
        model_key=comparison.model_key,
        prompt_version=comparison.prompt_version,
        schema_version=schema_version,
        runtime=payload.runtime,
        candidate_asset_ids=candidate_asset_ids,
    )
