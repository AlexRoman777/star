from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    Asset,
    EvidenceEmbedding,
    Task,
    VisualFingerprint,
)
from star.domain.enums import TaskState
from star.domain.schemas import (
    InferenceInvocationSubmission,
    SemanticEmbeddingResultSubmission,
)
from star.services.campaigns import canonical_digest
from star.services.inference_runs import record_inference_invocations

PROCESSOR_VERSION = "dinov2-small-384-v1"
MODEL_KEY = "facebook/dinov2-small"


async def record_semantic_embedding_submission(
    session: AsyncSession,
    *,
    task: Task,
    payload: SemanticEmbeddingResultSubmission,
) -> AnalysisRun:
    if task.task_type != "asset.semantic_embedding":
        raise ValueError("task does not accept semantic embedding results")
    asset_id = uuid.UUID(str(task.target_ref.get("asset_id")))
    revision_id = uuid.UUID(str(task.target_ref.get("asset_revision_id")))
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.current_revision_id != revision_id:
        task.state = TaskState.CANCELLED
        task.last_error_class = "AssetRevisionSuperseded"
        raise RuntimeError("asset changed while semantic embedding was running")

    run = await session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.analysis_generation_id == task.analysis_generation_id,
            AnalysisRun.asset_id == asset_id,
            AnalysisRun.asset_revision_id == revision_id,
            AnalysisRun.analysis_type == "semantic_embedding",
            AnalysisRun.processor_version == PROCESSOR_VERSION,
            AnalysisRun.model_key == MODEL_KEY,
        )
    )
    if run is None:
        run = AnalysisRun(
            analysis_generation_id=task.analysis_generation_id,
            campaign_id=task.campaign_id,
            asset_id=asset_id,
            asset_revision_id=revision_id,
            analysis_type="semantic_embedding",
            processor_version=PROCESSOR_VERSION,
            model_key=MODEL_KEY,
            status="running",
            limitations=["relationship_evidence_only", "immich_preview_derivative"],
        )
        session.add(run)
        await session.flush()
    result_document = payload.model_dump(mode="json")
    result_digest = canonical_digest(result_document)
    already_completed = run.status == "succeeded"
    if already_completed and run.result_digest != result_digest:
        raise RuntimeError("completed visual embedding result is immutable")

    embedding = await session.scalar(
        select(EvidenceEmbedding).where(
            EvidenceEmbedding.analysis_run_id == run.id,
            EvidenceEmbedding.purpose == "visual_similarity",
        )
    )
    encoded = json.dumps(payload.embedding, separators=(",", ":"))
    if embedding is None:
        session.add(
            EvidenceEmbedding(
                asset_id=asset_id,
                asset_revision_id=revision_id,
                analysis_run_id=run.id,
                analysis_generation_id=task.analysis_generation_id,
                purpose="visual_similarity",
                algorithm="dinov2",
                algorithm_version=PROCESSOR_VERSION,
                model_key=payload.model_key,
                model_revision=payload.model_revision,
                input_digest=payload.input_digest,
                embedding=encoded,
            )
        )
    elif embedding.input_digest != payload.input_digest or embedding.embedding != encoded:
        raise RuntimeError("completed visual embedding evidence is immutable")

    if payload.fingerprints is not None:
        # Since v209 round 3 the similarity task carries the deterministic
        # layout fingerprints the retired Technical preview used to record.
        submitted = payload.fingerprints
        fingerprint_values = (
            ("phash", "imagehash-4.3.2-hash8", submitted.perceptual_hash, 64),
            ("phash", "imagehash-4.3.2-hash16", submitted.perceptual_hash_256, 256),
            ("dhash", "imagehash-4.3.2-hash16", submitted.difference_hash_256, 256),
            ("color-grid", "rgb-4x4-v1", submitted.color_signature, 384),
            ("ssim-grid", "luma-16x16-v1", submitted.ssim_luma_signature, 2048),
        )
        for algorithm, algorithm_version, value, bit_length in fingerprint_values:
            fingerprint = await session.scalar(
                select(VisualFingerprint).where(
                    VisualFingerprint.asset_id == asset_id,
                    VisualFingerprint.asset_revision_id == revision_id,
                    VisualFingerprint.algorithm == algorithm,
                    VisualFingerprint.algorithm_version == algorithm_version,
                )
            )
            if fingerprint is None:
                session.add(
                    VisualFingerprint(
                        asset_id=asset_id,
                        asset_revision_id=revision_id,
                        analysis_run_id=run.id,
                        algorithm=algorithm,
                        algorithm_version=algorithm_version,
                        value=value,
                        bit_length=bit_length,
                    )
                )
            else:
                fingerprint.analysis_run_id = run.id
                fingerprint.value = value
                fingerprint.bit_length = bit_length
                fingerprint.created_at = utc_now()
        await session.flush()
    fingerprints = list(
        await session.scalars(
            select(VisualFingerprint).where(
                VisualFingerprint.asset_id == asset_id,
                VisualFingerprint.asset_revision_id == revision_id,
            )
        )
    )
    for fingerprint in fingerprints:
        fingerprint.similarity_checked_at = None

    run.status = "succeeded"
    run.input_provenance = payload.media_provenance.model_dump(mode="json")
    if not already_completed:
        run.worker_runtime_ms = int(run.worker_runtime_ms or 0) + payload.duration_ms
    run.error_class = None
    run.result_payload = result_document
    run.result_digest = result_digest
    run.completed_at = utc_now()
    await record_inference_invocations(
        session,
        task=task,
        analysis_run=run,
        model_key=payload.model_key,
        invocations=[
            InferenceInvocationSubmission(
                phase="visual_embedding",
                model_reported_version=payload.model_revision,
                runtime="transformers-local",
                prompt_version="none",
                schema_version=PROCESSOR_VERSION,
                input_digest=payload.input_digest,
                output_digest=canonical_digest(payload.embedding),
                image_count=1,
                duration_ms=payload.duration_ms,
            )
        ],
    )
    task.state = TaskState.SUCCEEDED
    completing_worker_id = (
        str(task.leased_to_worker_id) if task.leased_to_worker_id is not None else None
    )
    task.progress = {
        "stage": "semantic_embedding",
        "analysis_run_id": str(run.id),
        "algorithm_version": PROCESSOR_VERSION,
        "dimensions": len(payload.embedding),
        "duration_ms": payload.duration_ms,
        "completed_worker_id": completing_worker_id,
        "last_worker_id": completing_worker_id,
        "completed_lease_id": str(task.lease_id) if task.lease_id is not None else None,
    }
    task.lease_id = None
    task.lease_expires_at = None
    task.leased_to_worker_id = None
    await session.flush()
    return run
