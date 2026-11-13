from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    CollectionGroup,
    CollectionGroupMember,
    DeepReviewPromptResult,
    EvidenceEmbedding,
    HighlightComparison,
    Proposal,
    Task,
    VisualFingerprint,
    VisualFingerprintBand,
)
from star.domain.enums import ProposalState, ProposalType, TaskState
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.comparison_compute import record_group_compute_allocations
from star.services.evidence_persistence import seal_comparison_evidence
from star.services.family_context import trusted_family_context
from star.services.inference_runs import record_inference_invocations
from star.services.pipeline_contract import canonical_completion_role
from star.services.similarity_confidence import explain_similarity_confidence

PHASH_VERSION = "imagehash-4.3.2-hash16"
DHASH_VERSION = "imagehash-4.3.2-hash16"
SERIES_VERSION = "visual-series-v4-visual-description-session-graph"
EMBEDDING_VERSION = "dinov2-small-384-v1"
DESCRIPTION_EMBEDDING_VERSION = "all-minilm-l6-v2-description-v1"
SSIM_VERSION = "luma-16x16-v1"
BAND_HEX_WIDTH = 4
SERIES_SELECTOR_MODEL_KEY = "qwen3.8:27b"
SERIES_SELECTOR_PROMPT_VERSION = "series-selector-v2-family-memory-representative"
DEEP_CURATION_PROMPT_VERSION = "deep-curation-v1"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _capture_time(metadata: dict[str, Any]) -> datetime | None:
    values: list[object] = [
        metadata.get("localDateTime"),
        metadata.get("fileCreatedAt"),
        metadata.get("dateTimeOriginal"),
    ]
    exif = metadata.get("exifInfo")
    if isinstance(exif, dict):
        values.extend((exif.get("dateTimeOriginal"), exif.get("modifyDate")))
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _camera_key(metadata: dict[str, Any]) -> str:
    exif = metadata.get("exifInfo")
    if not isinstance(exif, dict):
        return "unknown"
    return f"{exif.get('make', '')}:{exif.get('model', '')}".strip(":").lower() or "unknown"


def _color_distance(left: str | None, right: str | None) -> float:
    if not left or not right or len(left) != len(right):
        return 255.0
    left_bytes = bytes.fromhex(left)
    right_bytes = bytes.fromhex(right)
    return sum(abs(a - b) for a, b in zip(left_bytes, right_bytes, strict=True)) / len(left_bytes)


def _ssim(left: str | None, right: str | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_values = list(bytes.fromhex(left))
    right_values = list(bytes.fromhex(right))
    count = len(left_values)
    if count < 2:
        return 0.0
    left_mean = sum(left_values) / count
    right_mean = sum(right_values) / count
    left_var = sum((value - left_mean) ** 2 for value in left_values) / (count - 1)
    right_var = sum((value - right_mean) ** 2 for value in right_values) / (count - 1)
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_values, right_values, strict=True)
    ) / (count - 1)
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    denominator = (left_mean**2 + right_mean**2 + c1) * (left_var + right_var + c2)
    if denominator <= 0:
        return 0.0
    score = ((2 * left_mean * right_mean + c1) * (2 * covariance + c2)) / denominator
    return max(0.0, min(1.0, score))


def _cosine_distance(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 2.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 2.0
    return float(max(0.0, min(2.0, 1.0 - dot / (left_norm * right_norm))))


def _parse_vector(value: object) -> list[float]:
    if not isinstance(value, str):
        return []
    stripped = value.strip().removeprefix("[").removesuffix("]")
    try:
        return [float(item) for item in stripped.split(",") if item]
    except ValueError:
        return []


async def _embedding_candidates(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    revision_id: uuid.UUID,
    purpose: str,
    version: str,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[float]]]:
    if session.get_bind().dialect.name != "postgresql":
        rows = list(
            await session.scalars(
                select(EvidenceEmbedding).where(
                    EvidenceEmbedding.purpose == purpose,
                    EvidenceEmbedding.algorithm_version == version,
                )
            )
        )
        latest: dict[uuid.UUID, EvidenceEmbedding] = {}
        for row in sorted(rows, key=lambda value: (value.created_at, value.id), reverse=True):
            latest.setdefault(row.asset_id, row)
        current = next(
            (
                item
                for item in latest.values()
                if item.asset_id == asset_id and item.asset_revision_id == revision_id
            ),
            None,
        )
        if current is None:
            return [], {}
        vectors = {item.asset_id: _parse_vector(item.embedding) for item in latest.values()}
        source = vectors[asset_id]
        ordered = sorted(
            (item.asset_id for item in latest.values() if item.asset_id != asset_id),
            key=lambda candidate_id: (
                _cosine_distance(source, vectors[candidate_id]),
                str(candidate_id),
            ),
        )[:96]
        return ordered, vectors
    current_row = await session.execute(
        text(
            "SELECT embedding::text FROM visual_embeddings "
            "WHERE asset_id=:asset_id AND asset_revision_id=:revision_id "
            "AND purpose=:purpose AND algorithm_version=:version "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {
            "asset_id": asset_id,
            "revision_id": revision_id,
            "purpose": purpose,
            "version": version,
        },
    )
    embedding_text = current_row.scalar_one_or_none()
    if not isinstance(embedding_text, str):
        return [], {}
    neighbors = await session.execute(
        text(
            "WITH latest AS (SELECT DISTINCT ON (asset_id) asset_id, embedding, created_at "
            "FROM visual_embeddings WHERE asset_id<>:asset_id AND purpose=:purpose "
            "AND algorithm_version=:version ORDER BY asset_id, created_at DESC) "
            "SELECT asset_id, embedding::text FROM latest "
            "ORDER BY embedding <=> CAST(:embedding AS vector) LIMIT 96"
        ),
        {
            "asset_id": asset_id,
            "purpose": purpose,
            "version": version,
            "embedding": embedding_text,
        },
    )
    vectors = {asset_id: _parse_vector(embedding_text)}
    candidate_ids: list[uuid.UUID] = []
    for candidate_id, candidate_embedding in neighbors:
        normalized_id = uuid.UUID(str(candidate_id))
        candidate_ids.append(normalized_id)
        vectors[normalized_id] = _parse_vector(candidate_embedding)
    return candidate_ids, vectors


async def materialize_fingerprint_bands(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> int:
    already_banded = exists(
        select(VisualFingerprintBand.id).where(
            VisualFingerprintBand.fingerprint_id == VisualFingerprint.id
        )
    )
    fingerprints = list(
        await session.scalars(
            select(VisualFingerprint)
            .where(
                VisualFingerprint.algorithm == "phash",
                VisualFingerprint.algorithm_version == PHASH_VERSION,
                VisualFingerprint.bit_length == 256,
                ~already_banded,
            )
            .order_by(VisualFingerprint.created_at, VisualFingerprint.id)
            .limit(limit)
        )
    )
    for fingerprint in fingerprints:
        for index in range(0, len(fingerprint.value), BAND_HEX_WIDTH):
            session.add(
                VisualFingerprintBand(
                    fingerprint_id=fingerprint.id,
                    asset_id=fingerprint.asset_id,
                    algorithm_version=fingerprint.algorithm_version,
                    band_index=index // BAND_HEX_WIDTH,
                    band_value=fingerprint.value[index : index + BAND_HEX_WIDTH],
                )
            )
    if fingerprints:
        await session.flush()
    return len(fingerprints)


async def _candidate_asset_ids(
    session: AsyncSession,
    fingerprint: VisualFingerprint,
) -> list[uuid.UUID]:
    bands = [
        (index // BAND_HEX_WIDTH, fingerprint.value[index : index + BAND_HEX_WIDTH])
        for index in range(0, len(fingerprint.value), BAND_HEX_WIDTH)
    ]
    return list(
        await session.scalars(
            select(VisualFingerprintBand.asset_id)
            .where(
                VisualFingerprintBand.algorithm_version == PHASH_VERSION,
                tuple_(
                    VisualFingerprintBand.band_index,
                    VisualFingerprintBand.band_value,
                ).in_(bands),
                VisualFingerprintBand.asset_id != fingerprint.asset_id,
            )
            .group_by(VisualFingerprintBand.asset_id)
            .having(func.count(VisualFingerprintBand.id) >= 2)
            .limit(256)
        )
    )


async def _representative(
    session: AsyncSession,
    asset_ids: set[uuid.UUID],
) -> uuid.UUID:
    fingerprints = list(
        await session.scalars(
            select(VisualFingerprint).where(
                VisualFingerprint.asset_id.in_(asset_ids),
                VisualFingerprint.algorithm == "phash",
                VisualFingerprint.algorithm_version == PHASH_VERSION,
            )
        )
    )
    latest_fingerprint: dict[uuid.UUID, VisualFingerprint] = {}
    for fingerprint in sorted(fingerprints, key=lambda value: (value.created_at, value.id)):
        latest_fingerprint[fingerprint.asset_id] = fingerprint
    phash_distance = {
        asset_id: sum(
            _hamming(
                latest_fingerprint[asset_id].value,
                latest_fingerprint[other_id].value,
            )
            for other_id in asset_ids
            if other_id != asset_id
        )
        for asset_id in asset_ids
        if set(latest_fingerprint) == asset_ids
    }
    proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.target_id.in_(asset_ids),
                Proposal.target_type == "asset",
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
            )
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
        )
    )
    latest: dict[uuid.UUID, Proposal] = {}
    for proposal in proposals:
        latest.setdefault(proposal.target_id, proposal)

    def rank_key(asset_id: uuid.UUID) -> tuple[float, int, int, int, int, int, int, str]:
        proposal = latest.get(asset_id)
        if proposal is None:
            centrality = 1.0 - phash_distance.get(asset_id, 64) / max(
                64 * max(1, len(asset_ids) - 1), 1
            )
            return (centrality * 25.0, 0, 0, 0, 0, 0, 0, str(asset_id))
        value = proposal.value
        observations = value.get("observations", value.get("observation_dossier"))
        if not isinstance(observations, dict):
            observations = {}
        frame_ready = int(observations.get("frame_function") in {"deliberate", "documentary"})
        eyes_ready = int(observations.get("eye_state") in {"open", "intentional_closed"})
        pose_ready = int(observations.get("pose_readiness") in {"ready", "not_applicable"})
        timing_ready = int(observations.get("timing_quality") in {"excellent", "good"})
        face_ready = int(observations.get("face_focus") in {"excellent", "good", "not_applicable"})
        defect_free = int(
            observations.get("important_face_or_head_cutoff") in {"none", "not_applicable"}
            and observations.get("subject_overlap") in {"none", "minor", "not_applicable"}
            and observations.get("subject_obstruction") in {"none", "minor", "not_applicable"}
        )
        centrality = 1.0 - phash_distance.get(asset_id, 64) / max(
            64 * max(1, len(asset_ids) - 1), 1
        )
        quality = (
            frame_ready * 15
            + eyes_ready * 15
            + pose_ready * 12
            + timing_ready * 12
            + face_ready * 12
            + defect_free * 12
            + float(value.get("adjusted_score", 0)) * 0.22
        )
        return (
            quality * 0.75 + centrality * 25.0,
            int(not bool(value.get("hard_cap_reasons"))),
            frame_ready,
            eyes_ready,
            pose_ready,
            timing_ready + face_ready,
            defect_free,
            str(asset_id),
        )

    return max(
        asset_ids,
        key=rank_key,
    )


def _balanced_series_batches(candidates: list[uuid.UUID]) -> list[list[uuid.UUID]]:
    """Cover every candidate once while keeping each comparison at 2-4 images."""

    if len(candidates) < 2:
        return []
    batch_count = (len(candidates) + 3) // 4
    base_size, extra = divmod(len(candidates), batch_count)
    batches: list[list[uuid.UUID]] = []
    offset = 0
    for index in range(batch_count):
        size = base_size + (1 if index < extra else 0)
        batches.append(candidates[offset : offset + size])
        offset += size
    return batches


async def _schedule_series_round(
    session: AsyncSession,
    *,
    group: CollectionGroup,
    assets: dict[uuid.UUID, Asset],
    candidates: list[uuid.UUID],
    round_number: int,
    experiment_id: uuid.UUID | None = None,
    hold: bool = False,
) -> int:
    batches = _balanced_series_batches(candidates)
    for batch_index, batch in enumerate(batches):
        candidate_context = [
            {
                "candidate_index": index,
                "trusted_family_context": await trusted_family_context(session, asset_id),
            }
            for index, asset_id in enumerate(batch, start=1)
        ]
        identity = {
            "type": "group.series_select",
            "group_id": str(group.id),
            "group_key": group.group_key,
            "detection_version": group.detection_version,
            "asset_ids": [str(asset_id) for asset_id in batch],
            "model_key": SERIES_SELECTOR_MODEL_KEY,
            "prompt_version": SERIES_SELECTOR_PROMPT_VERSION,
            "round_number": round_number,
            "batch_index": batch_index,
            "batch_count": len(batches),
            "experiment_id": (str(experiment_id) if experiment_id is not None else None),
        }
        idempotency_key = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
        if await session.scalar(select(Task.id).where(Task.idempotency_key == idempotency_key)):
            continue
        first = assets[batch[0]]
        session.add(
            Task(
                connection_id=first.connection_id,
                task_type="group.series_select",
                routing_key="medium-vlm",
                data_class="family-preview",
                estimated_work_class="long",
                completion_role=canonical_completion_role("group.series_select"),
                target_ref={
                    **identity,
                    "shuffle_seed": idempotency_key[:16],
                    "group_type": "visual_series",
                    "queue_origin": "similarity",
                    "apple_queue_category": "series",
                    "compute_budget_ms": 180_000,
                    "context_tokens": 16_384,
                    "max_completion_tokens": 1_024,
                    "reasoning_effort": "medium",
                    "candidate_context": candidate_context,
                    "photo_compute_allocation": "equal_per_candidate",
                },
                requirements={
                    "capabilities": ["mlx"],
                    "sensitivity": "family-media",
                    "input": f"{len(batch)}-separate-labelled-images",
                    "proposal_only": True,
                },
                idempotency_key=idempotency_key,
                state=TaskState.PLANNED if hold else TaskState.QUEUED,
                priority=max(5, 16 - round_number),
                max_attempts=3,
            )
        )
    return len(batches)


async def record_series_selection(
    session: AsyncSession,
    *,
    task: Task,
    payload: HighlightComparisonResultSubmission,
) -> CollectionGroup:
    if task.task_type != "group.series_select" or task.state != TaskState.RUNNING:
        raise ValueError("task does not accept a series selection result")
    group = await session.get(CollectionGroup, uuid.UUID(str(task.target_ref.get("group_id"))))
    if (
        group is None
        or group.state != "active"
        or group.group_key != task.target_ref.get("group_key")
        or group.detection_version != task.target_ref.get("detection_version")
    ):
        task.state = TaskState.CANCELLED
        task.last_error_class = "SeriesSuperseded"
        task.lease_id = None
        task.lease_expires_at = None
        task.leased_to_worker_id = None
        raise RuntimeError("visual series changed while selection was leased")
    asset_ids = [uuid.UUID(str(value)) for value in task.target_ref.get("asset_ids", [])]
    if not 2 <= len(asset_ids) <= 4:
        raise ValueError("series selection requires between two and four candidates")
    experiment_id = task.target_ref.get("experiment_id")
    if experiment_id:
        successful_asset_ids = set(
            await session.scalars(
                select(DeepReviewPromptResult.asset_id).where(
                    DeepReviewPromptResult.experiment_id == uuid.UUID(str(experiment_id)),
                    DeepReviewPromptResult.prompt_version == DEEP_CURATION_PROMPT_VERSION,
                    DeepReviewPromptResult.status == "succeeded",
                    DeepReviewPromptResult.asset_id.in_(asset_ids),
                )
            )
        )
        if successful_asset_ids != set(asset_ids):
            raise ValueError(
                "experimental series selection requires successful individual results "
                "from the same experiment"
            )
    expected = set(range(1, len(asset_ids) + 1))
    if {item.candidate_index for item in payload.ranking} != expected:
        raise ValueError("series selection must rank every candidate exactly once")
    ordered = sorted(payload.ranking, key=lambda item: (-item.quality, item.candidate_index))
    winner = asset_ids[ordered[0].candidate_index - 1]
    quality_spread = (ordered[0].quality - ordered[-1].quality) / 100.0
    comparison_generation = (
        f"series-experiment:{experiment_id}:{group.id}" if experiment_id else f"series:{group.id}"
    )
    comparison = HighlightComparison(
        task_id=task.id,
        generation=comparison_generation,
        round_kind=f"series_{int(task.target_ref.get('round_number', 1))}",
        model_key=SERIES_SELECTOR_MODEL_KEY,
        prompt_version=SERIES_SELECTOR_PROMPT_VERSION,
        candidate_ids=[str(value) for value in asset_ids],
        shuffle_seed=str(task.target_ref.get("shuffle_seed", task.id.hex[:16])),
        state="succeeded",
        ranking=[item.model_dump(mode="json", exclude_none=True) for item in ordered],
        input_provenance=[item.model_dump(mode="json") for item in payload.media_provenance],
        confidence=0.65 + 0.35 * max(0.0, min(1.0, quality_spread)),
        none_are_highlights=None,
        explanation=None,
        runtime=payload.runtime,
        model_reported_version=payload.model_reported_version,
        duration_ms=payload.duration_ms,
        completed_at=datetime.now(UTC),
    )
    session.add(comparison)
    await seal_comparison_evidence(
        session,
        task=task,
        comparison=comparison,
        payload=payload,
        candidate_asset_ids=asset_ids,
    )
    await record_inference_invocations(
        session,
        task=task,
        invocations=payload.inference_runs,
        model_key=SERIES_SELECTOR_MODEL_KEY,
    )
    await record_group_compute_allocations(
        session,
        task=task,
        payload=payload,
        candidate_ids=asset_ids,
        analysis_type="series_comparison_allocation",
        processor_version=SERIES_SELECTOR_PROMPT_VERSION,
        model_key=SERIES_SELECTOR_MODEL_KEY,
    )
    task.state = TaskState.SUCCEEDED
    task.progress = {
        "stage": "series_selection",
        "group_id": str(group.id),
        "round_number": int(task.target_ref.get("round_number", 1)),
        "round_winner_asset_id": str(winner),
        "comparison_id": str(comparison.id),
    }
    task.lease_id = None
    task.lease_expires_at = None
    task.leased_to_worker_id = None
    task.last_error_class = None
    await session.flush()

    round_number = int(task.target_ref.get("round_number", 1))
    batch_count = int(task.target_ref.get("batch_count", 1))
    all_series_tasks = list(
        await session.scalars(
            select(Task)
            .where(Task.task_type == "group.series_select")
            .order_by(Task.created_at, Task.id)
        )
    )
    round_tasks = [
        candidate
        for candidate in all_series_tasks
        if candidate.target_ref.get("group_id") == str(group.id)
        and candidate.target_ref.get("group_key") == group.group_key
        and candidate.target_ref.get("detection_version") == group.detection_version
        and candidate.target_ref.get("experiment_id") == experiment_id
        and int(candidate.target_ref.get("round_number", 1)) == round_number
    ]
    if len(round_tasks) != batch_count or any(
        candidate.state != TaskState.SUCCEEDED for candidate in round_tasks
    ):
        return group
    winners = [
        uuid.UUID(str(candidate.progress["round_winner_asset_id"])) for candidate in round_tasks
    ]
    if len(winners) > 1:
        winner_assets = {
            item.id: item
            for item in await session.scalars(select(Asset).where(Asset.id.in_(winners)))
        }
        await _schedule_series_round(
            session,
            group=group,
            assets=winner_assets,
            candidates=winners,
            round_number=round_number + 1,
            experiment_id=(uuid.UUID(str(experiment_id)) if experiment_id else None),
        )
        if not experiment_id:
            group.source_facts = {
                **group.source_facts,
                "selector_state": "tournament_running",
                "selector_round": round_number + 1,
            }
        await session.flush()
        return group

    final_winner = winners[0]
    comparisons = list(
        await session.scalars(
            select(HighlightComparison)
            .where(HighlightComparison.generation == comparison_generation)
            .order_by(HighlightComparison.completed_at, HighlightComparison.id)
        )
    )
    if experiment_id:
        # Owner-requested deep curation is retained as advisory evidence. It must
        # never replace the canonical similarity representative or member roles.
        task.progress = {
            **task.progress,
            "stage": "series_experiment_complete",
            "experiment_id": str(experiment_id),
            "advisory_representative_asset_id": str(final_winner),
            "comparison_ids": [str(value.id) for value in comparisons],
        }
        await session.flush()
        return group

    group.representative_asset_id = final_winner
    group.source_facts = {
        **group.source_facts,
        "representative_method": "blind_qwen3_vl_full_series_tournament",
        "selector_state": "complete",
        "selector_model": SERIES_SELECTOR_MODEL_KEY,
        "selector_prompt_version": SERIES_SELECTOR_PROMPT_VERSION,
        "selector_rounds": round_number,
        "selector_comparison_ids": [str(value.id) for value in comparisons],
    }
    members = list(
        await session.scalars(
            select(CollectionGroupMember).where(CollectionGroupMember.group_id == group.id)
        )
    )
    for member in members:
        member.role = "representative" if member.asset_id == final_winner else "alternate"
    task.progress = {
        **task.progress,
        "stage": "series_tournament_complete",
        "representative_asset_id": str(final_winner),
    }
    await session.flush()
    return group


async def _store_series(
    session: AsyncSession,
    *,
    assets: dict[uuid.UUID, Asset],
    revisions: dict[uuid.UUID, AssetRevision],
    member_ids: set[uuid.UUID],
    distances: dict[str, int],
    semantic_embedding_available: bool,
    description_embedding_available: bool,
) -> None:
    confidence = explain_similarity_confidence(
        distances,
        member_count=len(member_ids),
        semantic_embedding_available=semantic_embedding_available,
    )
    overlapping = list(
        await session.scalars(
            select(CollectionGroup)
            .join(CollectionGroupMember, CollectionGroupMember.group_id == CollectionGroup.id)
            .where(
                CollectionGroup.group_type == "visual_series",
                CollectionGroup.state == "active",
                CollectionGroupMember.asset_id.in_(member_ids),
            )
            .distinct()
        )
    )
    for overlap in overlapping:
        existing = set(
            await session.scalars(
                select(CollectionGroupMember.asset_id).where(
                    CollectionGroupMember.group_id == overlap.id
                )
            )
        )
        # A shared member cannot bridge two clusters. Only replace a prior group when the
        # newly verified complete-link candidate set contains every prior member.
        if existing.issubset(member_ids):
            overlap.state = "superseded"
        else:
            member_ids.difference_update(existing)
    if len(member_ids) < 2:
        return
    missing_ids = member_ids - set(assets)
    if missing_ids:
        missing_rows = list(
            await session.execute(
                select(Asset, AssetRevision)
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .where(Asset.id.in_(missing_ids))
            )
        )
        for missing_asset, missing_revision in missing_rows:
            assets[missing_asset.id] = missing_asset
            revisions[missing_asset.id] = missing_revision
        member_ids.intersection_update(assets)
    identity = ":".join(sorted(str(value) for value in member_ids))
    key = hashlib.sha256(f"visual_series:{identity}".encode()).hexdigest()
    representative = await _representative(session, member_ids)
    first = assets[next(iter(member_ids))]
    group = await session.scalar(
        select(CollectionGroup).where(
            CollectionGroup.group_type == "visual_series",
            CollectionGroup.group_key == key,
        )
    )
    if group is None:
        group = CollectionGroup(
            connection_id=first.connection_id,
            group_type="visual_series",
            group_key=key,
            detection_version=SERIES_VERSION,
            confidence=confidence.confidence,
            state="active",
            explanation="Preview hashes and capture context identify a near-duplicate series.",
            source_facts={
                "distances": distances,
                "preview_only": True,
                "semantic_embedding_version": (
                    EMBEDDING_VERSION if semantic_embedding_available else None
                ),
                "description_embedding_version": (
                    DESCRIPTION_EMBEDDING_VERSION if description_embedding_available else None
                ),
                "embedding_purposes": [
                    purpose
                    for purpose, available in (
                        ("visual_similarity", semantic_embedding_available),
                        ("description_similarity", description_embedding_available),
                    )
                    if available
                ],
                "representative_method": "phash_medoid_with_review_fallback",
                "confidence_model": confidence.payload(),
            },
            representative_asset_id=representative,
            member_count=len(member_ids),
        )
        session.add(group)
        await session.flush()
    else:
        group.state = "active"
        group.detection_version = SERIES_VERSION
        group.confidence = confidence.confidence
        group.source_facts = {
            "distances": distances,
            "preview_only": True,
            "semantic_embedding_version": (
                EMBEDDING_VERSION if semantic_embedding_available else None
            ),
            "description_embedding_version": (
                DESCRIPTION_EMBEDDING_VERSION if description_embedding_available else None
            ),
            "embedding_purposes": [
                purpose
                for purpose, available in (
                    ("visual_similarity", semantic_embedding_available),
                    ("description_similarity", description_embedding_available),
                )
                if available
            ],
            "representative_method": "phash_medoid_with_review_fallback",
            "confidence_model": confidence.payload(),
        }
        group.representative_asset_id = representative
        group.member_count = len(member_ids)
        await session.execute(
            delete(CollectionGroupMember).where(CollectionGroupMember.group_id == group.id)
        )
    ordered = sorted(member_ids, key=str)
    for index, asset_id in enumerate(ordered):
        revision = revisions[asset_id]
        session.add(
            CollectionGroupMember(
                group_id=group.id,
                asset_id=asset_id,
                asset_revision_id=revision.id,
                sequence_index=index,
                role="representative" if asset_id == representative else "alternate",
                confidence=0.9,
                source_facts={"preview_only": True},
            )
        )
    group.source_facts = {
        **group.source_facts,
        "selector_state": "not_scheduled",
        "selector_reason": "deterministic_representative_avoids_apple_model_switch",
    }


async def reconcile_visual_series(
    session: AsyncSession,
    *,
    limit: int = 200,
    prepare_bands: bool = True,
) -> int:
    started = time.monotonic()
    if prepare_bands:
        await materialize_fingerprint_bands(session, limit=max(limit, 50))
    rows = list(
        await session.execute(
            select(VisualFingerprint, Asset, AssetRevision)
            .join(Asset, Asset.id == VisualFingerprint.asset_id)
            .join(AssetRevision, AssetRevision.id == VisualFingerprint.asset_revision_id)
            .where(
                VisualFingerprint.algorithm == "phash",
                VisualFingerprint.algorithm_version == PHASH_VERSION,
                VisualFingerprint.similarity_checked_at.is_(None),
                Asset.current_revision_id == VisualFingerprint.asset_revision_id,
                Asset.access_state == "available",
            )
            .order_by(VisualFingerprint.created_at, VisualFingerprint.id)
            .limit(limit)
        )
    )
    changed = 0
    for index, (fingerprint, asset, revision) in enumerate(rows):
        # Commit useful partial work at a photo boundary instead of letting
        # hundreds of optional comparisons monopolize the library cycle.
        if index and time.monotonic() - started >= 10:
            break
        candidate_ids = await _candidate_asset_ids(session, fingerprint)
        embedding_ids, embeddings = await _embedding_candidates(
            session,
            asset_id=asset.id,
            revision_id=revision.id,
            purpose="visual_similarity",
            version=EMBEDDING_VERSION,
        )
        description_ids, description_embeddings = await _embedding_candidates(
            session,
            asset_id=asset.id,
            revision_id=revision.id,
            purpose="description_similarity",
            version=DESCRIPTION_EMBEDDING_VERSION,
        )
        candidate_ids = list(dict.fromkeys([*candidate_ids, *embedding_ids, *description_ids]))
        all_ids = set(candidate_ids) | {asset.id}
        candidate_rows = list(
            await session.execute(
                select(Asset, AssetRevision, VisualFingerprint)
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .join(
                    VisualFingerprint,
                    (VisualFingerprint.asset_id == Asset.id)
                    & (VisualFingerprint.asset_revision_id == AssetRevision.id)
                    & (VisualFingerprint.algorithm == "phash")
                    & (VisualFingerprint.algorithm_version == PHASH_VERSION),
                )
                .where(Asset.id.in_(all_ids), Asset.connection_id == asset.connection_id)
            )
        )
        assets = {row_asset.id: row_asset for row_asset, _row_revision, _fp in candidate_rows}
        revisions = {row_asset.id: row_revision for row_asset, row_revision, _fp in candidate_rows}
        phashes = {row_asset.id: row_fp.value for row_asset, _revision, row_fp in candidate_rows}
        secondary = list(
            await session.scalars(
                select(VisualFingerprint).where(
                    VisualFingerprint.asset_id.in_(assets),
                    VisualFingerprint.asset_revision_id.in_(
                        [value.id for value in revisions.values()]
                    ),
                    VisualFingerprint.algorithm.in_({"dhash", "color-grid", "ssim-grid"}),
                )
            )
        )
        dhashes = {value.asset_id: value.value for value in secondary if value.algorithm == "dhash"}
        colors = {
            value.asset_id: value.value for value in secondary if value.algorithm == "color-grid"
        }
        ssim_signatures = {
            value.asset_id: value.value
            for value in secondary
            if value.algorithm == "ssim-grid" and value.algorithm_version == SSIM_VERSION
        }
        direct_matches: list[uuid.UUID] = []
        distances: dict[str, int] = {}

        def pair_is_close(
            left_id: uuid.UUID,
            right_id: uuid.UUID,
            context: tuple[Any, ...] = (
                revisions,
                phashes,
                dhashes,
                colors,
                ssim_signatures,
                embeddings,
                description_embeddings,
            ),
        ) -> bool:
            (
                revisions,
                phashes,
                dhashes,
                colors,
                ssim_signatures,
                embeddings,
                description_embeddings,
            ) = context
            left_revision = revisions.get(left_id)
            right_revision = revisions.get(right_id)
            left_phash = phashes.get(left_id)
            right_phash = phashes.get(right_id)
            if left_revision is None or right_revision is None or not left_phash or not right_phash:
                return False
            phash_distance = _hamming(left_phash, right_phash)
            dhash_distance = _hamming(
                dhashes.get(left_id, "0" * 64), dhashes.get(right_id, "f" * 64)
            )
            color_distance = _color_distance(colors.get(left_id), colors.get(right_id))
            ssim_score = _ssim(ssim_signatures.get(left_id), ssim_signatures.get(right_id))
            embedding_distance = _cosine_distance(embeddings.get(left_id), embeddings.get(right_id))
            description_distance = _cosine_distance(
                description_embeddings.get(left_id),
                description_embeddings.get(right_id),
            )
            left_capture = _capture_time(left_revision.normalized_metadata)
            right_capture = _capture_time(right_revision.normalized_metadata)
            same_session = bool(
                left_capture is not None
                and right_capture is not None
                and abs((left_capture - right_capture).total_seconds()) <= 15 * 60
                and _camera_key(left_revision.normalized_metadata)
                == _camera_key(right_revision.normalized_metadata)
            )
            close_in_time = bool(
                same_session
                and left_capture is not None
                and right_capture is not None
                and abs((left_capture - right_capture).total_seconds()) <= 60
            )
            has_semantic_pair = left_id in embeddings and right_id in embeddings
            has_description_pair = (
                left_id in description_embeddings and right_id in description_embeddings
            )
            strict_match = (
                phash_distance <= (18 if has_semantic_pair else 10)
                and dhash_distance <= (32 if has_semantic_pair else 20)
                and color_distance <= (52 if has_semantic_pair else 40)
                and ssim_score >= (0.82 if has_semantic_pair else 0.90)
                and (not has_semantic_pair or embedding_distance <= 0.34)
            )
            burst_match = (
                close_in_time
                and phash_distance <= 38
                and dhash_distance <= 52
                and color_distance <= 72
                and ssim_score >= 0.68
                and (not has_semantic_pair or embedding_distance <= 0.48)
            )
            sequence_match = (
                same_session
                and has_semantic_pair
                and has_description_pair
                and embedding_distance <= 0.55
                and description_distance <= 0.30
                and phash_distance <= 48
                and dhash_distance <= 64
                and color_distance <= 86
                and ssim_score >= 0.58
            )
            return strict_match or burst_match or sequence_match

        for candidate_id in candidate_ids:
            candidate_phash = phashes.get(candidate_id)
            if candidate_phash is None:
                continue
            if pair_is_close(asset.id, candidate_id):
                direct_matches.append(candidate_id)
                distances[str(candidate_id)] = _hamming(fingerprint.value, candidate_phash)
        matches: set[uuid.UUID] = {asset.id}
        frontier = [asset.id]
        remaining = set(candidate_ids)
        while frontier:
            member_id = frontier.pop(0)
            newly_connected = {
                candidate_id for candidate_id in remaining if pair_is_close(member_id, candidate_id)
            }
            for candidate_id in sorted(newly_connected, key=str):
                remaining.remove(candidate_id)
                matches.add(candidate_id)
                frontier.append(candidate_id)
        fingerprint.similarity_checked_at = utc_now()
        fingerprint.similarity_neighbor_count = max(0, len(matches) - 1)
        if len(matches) >= 2:
            await _store_series(
                session,
                assets=assets,
                revisions=revisions,
                member_ids=matches,
                distances=distances,
                semantic_embedding_available=all(member_id in embeddings for member_id in matches),
                description_embedding_available=all(
                    member_id in description_embeddings for member_id in matches
                ),
            )
            changed += 1
    if rows:
        await session.flush()
    return changed
