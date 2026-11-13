from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from math import ceil
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    Asset,
    AssetRevision,
    HumanDecision,
    InferenceRun,
    LibraryProgram,
    ModelPackage,
    ModelVersion,
    Proposal,
    Worker,
    WorkerModelAvailability,
)
from star.db.session import get_session
from star.domain.enums import DecisionType, ProposalType, Role
from star.domain.schemas import (
    ModelBenchmarkUpdate,
    ModelPackageCreate,
    ModelPackageResponse,
    ModelPerformanceResponse,
    ModelVersionCreate,
    ModelVersionResponse,
    ModelWorkerAvailabilityResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_program import effective_inference_settings
from star.services.model_catalog import active_settings_for_model, model_build_details
from star.services.model_packages import verify_model_package
from star.services.operation_locks import lock_operation_family
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_contract import PRODUCTION_MODEL_KEYS
from star.services.storage import get_storage_status

router = APIRouter(prefix="/models", tags=["Models"])
admin = require_roles(Role.ADMINISTRATOR)
viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)

SOURCE_WEIGHTS = {
    "primary_photo_review": 1.0,
    "deep_photo_adjudication": 1.0,
}
LEAN_RATING_SOURCES = {"primary_photo_review", "deep_photo_adjudication"}


@dataclass(slots=True)
class PerformanceBucket:
    votes: int = 0
    comparisons: int = 0
    exact: int = 0
    within_one: int = 0
    absolute_error: float = 0.0
    signed_error: float = 0.0
    duration_sum: float = 0.0
    duration_count: int = 0
    fastest_duration: int | None = None
    slowest_duration: int | None = None
    durations: list[int] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    succeeded: int = 0
    failed: int = 0
    consensus_comparisons: int = 0
    consensus_exact: int = 0
    consensus_within_one: int = 0
    consensus_absolute_error: float = 0.0
    consensus_signed_error: float = 0.0
    immich_comparisons: int = 0
    immich_exact: int = 0
    immich_within_one: int = 0
    immich_absolute_error: float = 0.0
    favorite_comparisons: int = 0
    favorite_agreements: int = 0
    inference_runs: int = 0
    provider_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    costed_runs: int = 0
    estimated_cost_microunits: int = 0


def _proposal_model_key(proposal: Proposal, models: dict[str, ModelVersion]) -> str | None:
    value = proposal.value
    explicit = value.get("model_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    version_id = value.get("model_version_id")
    if version_id is None:
        return None
    model = models.get(str(version_id))
    return model.model_id if model is not None else str(version_id)


def _current_human_ratings(decisions: list[HumanDecision]) -> dict[uuid.UUID, int]:
    superseded = {item.supersedes_id for item in decisions if item.supersedes_id is not None}
    ratings: dict[uuid.UUID, int] = {}
    for decision in decisions:
        if decision.id in superseded or decision.decision_type != DecisionType.RATE:
            continue
        rating = decision.value.get("rating")
        if rating is None:
            ratings.pop(decision.target_id, None)
        elif isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5:
            ratings[decision.target_id] = rating
    return ratings


def _accumulate_leave_one_out_consensus(
    stats: dict[str, PerformanceBucket],
    votes_by_generation: dict[tuple[uuid.UUID, uuid.UUID], dict[str, int]],
) -> None:
    """Compare each voter with the median of the other model identities."""

    for votes in votes_by_generation.values():
        if len(votes) < 2:
            continue
        for model_key, rating in votes.items():
            peer_consensus = float(
                median(value for key, value in votes.items() if key != model_key)
            )
            error = rating - peer_consensus
            item = stats.setdefault(model_key, PerformanceBucket())
            item.consensus_comparisons += 1
            item.consensus_exact += int(error == 0)
            item.consensus_within_one += int(abs(error) <= 1)
            item.consensus_absolute_error += abs(error)
            item.consensus_signed_error += error


async def _immich_label_classes(
    session: AsyncSession,
    *,
    exclude: set[uuid.UUID],
) -> tuple[dict[uuid.UUID, int], set[uuid.UUID]]:
    query = select(Asset.id, AssetRevision.rating, AssetRevision.is_favorite).join(
        AssetRevision, AssetRevision.id == Asset.current_revision_id
    )
    if exclude:
        query = query.where(Asset.id.not_in(exclude))
    ratings: dict[uuid.UUID, int] = {}
    favorites: set[uuid.UUID] = set()
    for asset_id, rating, is_favorite in (await session.execute(query)).all():
        if isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5:
            ratings[asset_id] = rating
        if is_favorite:
            favorites.add(asset_id)
    return ratings, favorites


async def _accumulate_immich_labels(
    session: AsyncSession,
    *,
    bucket: Callable[[str], PerformanceBucket],
    models_by_id: dict[str, ModelVersion],
    immich_ratings: dict[uuid.UUID, int],
    immich_favorites: set[uuid.UUID],
) -> None:
    asset_ids = list(set(immich_ratings) | immich_favorites)
    for offset in range(0, len(asset_ids), 500):
        proposals = list(
            (
                await session.scalars(
                    select(Proposal)
                    .join(Asset, Asset.id == Proposal.target_id)
                    .where(
                        Proposal.proposal_type == ProposalType.RATING,
                        Proposal.target_type == "asset",
                        Proposal.target_id.in_(asset_ids[offset : offset + 500]),
                        Proposal.value["source"].as_string().in_(LEAN_RATING_SOURCES),
                        or_(
                            Asset.current_generation_id.is_(None),
                            Proposal.analysis_generation_id == Asset.current_generation_id,
                        ),
                    )
                )
            ).all()
        )
        for proposal in proposals:
            model_key = _proposal_model_key(proposal, models_by_id)
            proposed_rating = proposal.value.get("rating")
            if model_key is None or not isinstance(proposed_rating, int):
                continue
            item = bucket(model_key)
            immich_rating = immich_ratings.get(proposal.target_id)
            if immich_rating is not None:
                error = abs(proposed_rating - immich_rating)
                item.immich_comparisons += 1
                item.immich_exact += int(error == 0)
                item.immich_within_one += int(error <= 1)
                item.immich_absolute_error += error
            if proposal.target_id in immich_favorites:
                item.favorite_comparisons += 1
                item.favorite_agreements += int(proposed_rating >= 4)


@router.get("/performance", response_model=list[ModelPerformanceResponse])
async def model_performance(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[ModelPerformanceResponse]:
    """Summarize persisted model votes; correctness uses human-confirmed ratings only."""
    model_rows = list((await session.scalars(select(ModelVersion))).all())
    models_by_id = {str(model.id): model for model in model_rows}
    decisions = list(
        (
            await session.scalars(
                select(HumanDecision)
                .where(HumanDecision.target_type == "asset")
                .order_by(HumanDecision.created_at, HumanDecision.id)
            )
        ).all()
    )
    human_ratings = _current_human_ratings(decisions)
    stats: dict[str, PerformanceBucket] = {}

    def bucket(model_key: str) -> PerformanceBucket:
        return stats.setdefault(model_key, PerformanceBucket())

    model_key_expression = func.coalesce(
        Proposal.value["model_id"].as_string(),
        Proposal.value["model_version_id"].as_string(),
    )
    source_expression = Proposal.value["source"].as_string()
    duration_expression = cast(Proposal.value["duration_ms"].as_string(), Integer)
    vote_rows = (
        await session.execute(
            select(
                model_key_expression.label("model_key"),
                source_expression.label("source"),
                func.count(Proposal.id).label("vote_count"),
                func.count(duration_expression).label("duration_count"),
                func.avg(duration_expression).label("average_duration"),
                func.min(duration_expression).label("fastest_duration"),
                func.max(duration_expression).label("slowest_duration"),
            )
            .where(
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.target_type == "asset",
                model_key_expression.is_not(None),
                source_expression.in_(LEAN_RATING_SOURCES),
            )
            .group_by(model_key_expression, source_expression)
        )
    ).all()
    for row in vote_rows:
        raw_key = str(row.model_key)
        model = models_by_id.get(raw_key)
        model_key = model.model_id if model is not None else raw_key
        item = bucket(model_key)
        item.votes += int(row.vote_count)
        if row.source:
            item.sources.add(str(row.source))
        if row.average_duration is not None:
            duration_count = int(row.duration_count)
            item.duration_sum += float(row.average_duration) * duration_count
            item.duration_count += duration_count
            fastest = int(row.fastest_duration)
            slowest = int(row.slowest_duration)
            previous_fastest = item.fastest_duration
            previous_slowest = item.slowest_duration
            item.fastest_duration = (
                fastest if previous_fastest is None else min(int(previous_fastest), fastest)
            )
            item.slowest_duration = (
                slowest if previous_slowest is None else max(int(previous_slowest), slowest)
            )

    rating_proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.target_type == "asset",
                Proposal.value["source"].as_string().in_(LEAN_RATING_SOURCES),
            )
            .order_by(Proposal.created_at, Proposal.id)
        )
    )
    latest_votes: dict[tuple[uuid.UUID, uuid.UUID, str], int] = {}
    for proposal in rating_proposals:
        proposal_model_key = _proposal_model_key(proposal, models_by_id)
        rating = proposal.value.get("rating")
        duration = proposal.value.get("duration_ms")
        if (
            proposal_model_key is not None
            and isinstance(duration, int)
            and not isinstance(duration, bool)
        ):
            bucket(proposal_model_key).durations.append(max(0, duration))
        if (
            proposal_model_key is None
            or proposal.analysis_generation_id is None
            or not isinstance(rating, int)
            or isinstance(rating, bool)
        ):
            continue
        latest_votes[(proposal.target_id, proposal.analysis_generation_id, proposal_model_key)] = (
            rating
        )

    inference_rows = list(
        await session.scalars(
            select(InferenceRun)
            .where(InferenceRun.state == "succeeded")
            .order_by(InferenceRun.completed_at, InferenceRun.id)
        )
    )
    inference_by_model: dict[str, list[InferenceRun]] = {}
    for inference in inference_rows:
        inference_by_model.setdefault(inference.model_key, []).append(inference)
    for model_key, runs in inference_by_model.items():
        item = bucket(model_key)
        durations = [run.duration_ms for run in runs]
        item.inference_runs = len(runs)
        item.sources.update(run.phase for run in runs)
        item.provider_requests = sum(run.request_count for run in runs)
        item.input_tokens = sum(run.input_tokens or 0 for run in runs)
        item.output_tokens = sum(run.output_tokens or 0 for run in runs)
        item.costed_runs = sum(run.estimated_cost_microunits is not None for run in runs)
        item.estimated_cost_microunits = sum(run.estimated_cost_microunits or 0 for run in runs)
        item.duration_sum = float(sum(durations))
        item.duration_count = len(durations)
        item.durations = durations
        item.fastest_duration = min(durations)
        item.slowest_duration = max(durations)
    votes_by_generation: dict[tuple[uuid.UUID, uuid.UUID], dict[str, int]] = {}
    for (asset_id, generation_id, model_key), rating in latest_votes.items():
        votes_by_generation.setdefault((asset_id, generation_id), {})[model_key] = rating
    _accumulate_leave_one_out_consensus(stats, votes_by_generation)

    run_rows = (
        await session.execute(
            select(AnalysisRun.model_key, AnalysisRun.status, func.count(AnalysisRun.id))
            .where(AnalysisRun.model_key != "deterministic")
            .where(AnalysisRun.analysis_type.in_({"primary_review", "photo_deep_review"}))
            .group_by(AnalysisRun.model_key, AnalysisRun.status)
        )
    ).all()
    for model_key, run_status, count in run_rows:
        if not model_key:
            continue
        item = bucket(str(model_key))
        if run_status == "succeeded":
            item.succeeded += int(count)
        elif run_status == "failed":
            item.failed += int(count)

    human_asset_ids = list(human_ratings)
    for offset in range(0, len(human_asset_ids), 500):
        comparison_proposals = list(
            (
                await session.scalars(
                    select(Proposal)
                    .join(Asset, Asset.id == Proposal.target_id)
                    .where(
                        Proposal.proposal_type == ProposalType.RATING,
                        Proposal.target_type == "asset",
                        Proposal.target_id.in_(human_asset_ids[offset : offset + 500]),
                        Proposal.value["source"].as_string().in_(LEAN_RATING_SOURCES),
                        or_(
                            Asset.current_generation_id.is_(None),
                            Proposal.analysis_generation_id == Asset.current_generation_id,
                        ),
                    )
                )
            ).all()
        )
        for proposal in comparison_proposals:
            resolved_model_key = _proposal_model_key(proposal, models_by_id)
            proposed_rating = proposal.value.get("rating")
            human_rating = human_ratings.get(proposal.target_id)
            if (
                resolved_model_key is None
                or human_rating is None
                or not isinstance(proposed_rating, int)
            ):
                continue
            item = bucket(resolved_model_key)
            error = abs(proposed_rating - human_rating)
            item.comparisons += 1
            item.exact += int(error == 0)
            item.within_one += int(error <= 1)
            item.absolute_error += error
            item.signed_error += proposed_rating - human_rating

    # Immich's own stars and favourites are owner signal, but they are weaker labels than an
    # in-app confirmation: a star may predate the current crop and a favourite is only a binary
    # positive. They are therefore reported as separate label classes and never merged into the
    # human-confirmed accuracy figures.
    immich_ratings, immich_favorites = await _immich_label_classes(
        session, exclude=set(human_ratings)
    )
    await _accumulate_immich_labels(
        session,
        bucket=bucket,
        models_by_id=models_by_id,
        immich_ratings=immich_ratings,
        immich_favorites=immich_favorites,
    )

    models_by_key = {model.model_id: model for model in model_rows}
    response: list[ModelPerformanceResponse] = []
    for model_key, item in stats.items():
        model = models_by_key.get(model_key)
        sources = sorted(item.sources)
        duration_count = item.duration_count
        comparisons = item.comparisons
        consensus_comparisons = item.consensus_comparisons
        consensus_exact_rate = (
            item.consensus_exact / consensus_comparisons if consensus_comparisons else None
        )
        consensus_within_rate = (
            item.consensus_within_one / consensus_comparisons if consensus_comparisons else None
        )
        consensus_mae = (
            item.consensus_absolute_error / consensus_comparisons if consensus_comparisons else None
        )
        consensus_bias = (
            item.consensus_signed_error / consensus_comparisons if consensus_comparisons else None
        )
        maturity = (
            "established"
            if consensus_comparisons >= 1_000
            else "early"
            if consensus_comparisons >= 100
            else "collecting"
        )
        recommended_weight = None
        if (
            consensus_comparisons >= 100
            and consensus_within_rate is not None
            and consensus_mae is not None
        ):
            reliability = (
                (consensus_exact_rate or 0) * 0.4
                + consensus_within_rate * 0.4
                + max(0.0, 1 - consensus_mae / 2) * 0.2
            )
            recommended_weight = round(max(0.5, min(1.5, 0.5 + reliability)), 2)
        configured_weight = model.benchmark.get("consensus_weight") if model else None
        source_weight = max((SOURCE_WEIGHTS.get(source, 1.0) for source in sources), default=1.0)
        weight = (
            float(configured_weight)
            if isinstance(configured_weight, (int, float))
            else source_weight
        )
        response.append(
            ModelPerformanceResponse(
                model_key=model_key,
                display_name=model_key.rsplit("/", 1)[-1],
                runtime=model.runtime if model else None,
                version=model.version if model else None,
                enabled=model.enabled if model else True,
                role=(
                    "primary_rating"
                    if model_key == PRODUCTION_MODEL_KEYS["primary_rating"]
                    else "advisory_second_opinion"
                    if model_key == "qwen/qwen3.6-27b"
                    else "similarity_model"
                    if model_key
                    in {
                        "facebook/dinov2-small",
                        "sentence-transformers/all-MiniLM-L6-v2",
                    }
                    else "factual_detector"
                    if model_key == "apple-vision"
                    else "experimental_rating"
                ),
                proxy_method="leave_one_model_out_median",
                consensus_weight=max(0.05, min(2.0, weight)),
                vote_count=item.votes,
                human_comparison_count=comparisons,
                exact_match_count=item.exact,
                within_one_count=item.within_one,
                exact_match_rate=(item.exact / comparisons if comparisons else None),
                within_one_rate=(item.within_one / comparisons if comparisons else None),
                mean_absolute_error=(item.absolute_error / comparisons if comparisons else None),
                mean_error=(item.signed_error / comparisons if comparisons else None),
                consensus_comparison_count=consensus_comparisons,
                consensus_exact_match_rate=consensus_exact_rate,
                consensus_within_one_rate=consensus_within_rate,
                consensus_mean_absolute_error=consensus_mae,
                consensus_mean_error=consensus_bias,
                immich_rating_comparison_count=item.immich_comparisons,
                immich_rating_exact_match_rate=(
                    item.immich_exact / item.immich_comparisons if item.immich_comparisons else None
                ),
                immich_rating_within_one_rate=(
                    item.immich_within_one / item.immich_comparisons
                    if item.immich_comparisons
                    else None
                ),
                immich_rating_mean_absolute_error=(
                    item.immich_absolute_error / item.immich_comparisons
                    if item.immich_comparisons
                    else None
                ),
                immich_favorite_comparison_count=item.favorite_comparisons,
                immich_favorite_agreement_rate=(
                    item.favorite_agreements / item.favorite_comparisons
                    if item.favorite_comparisons
                    else None
                ),
                maturity=maturity,
                recommended_consensus_weight=recommended_weight,
                average_duration_ms=(
                    item.duration_sum / duration_count if duration_count else None
                ),
                median_duration_ms=(float(median(item.durations)) if item.durations else None),
                p95_duration_ms=(
                    float(sorted(item.durations)[max(0, ceil(len(item.durations) * 0.95) - 1)])
                    if item.durations
                    else None
                ),
                fastest_duration_ms=item.fastest_duration,
                slowest_duration_ms=item.slowest_duration,
                inference_run_count=item.inference_runs,
                provider_request_count=item.provider_requests,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                costed_run_count=item.costed_runs,
                estimated_cost_microunits=item.estimated_cost_microunits,
                succeeded_runs=item.succeeded,
                failed_runs=item.failed,
                sources=sources,
                evidence_note=(
                    f"Measured against {comparisons} human-confirmed rating"
                    f"{'s' if comparisons != 1 else ''}."
                    if comparisons
                    else (
                        "No human-confirmed comparison yet. Consensus agreement is a "
                        "useful proxy, not ground truth."
                    )
                ),
            )
        )
    return sorted(response, key=lambda item: (-item.vote_count, item.model_key))


@router.get("", response_model=list[ModelVersionResponse])
async def list_models(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[ModelVersionResponse]:
    models = list(
        (
            await session.scalars(
                select(ModelVersion).order_by(ModelVersion.model_id, ModelVersion.version)
            )
        ).all()
    )
    rows = list(
        (
            await session.execute(
                select(WorkerModelAvailability, Worker)
                .join(Worker, Worker.id == WorkerModelAvailability.worker_id)
                .where(Worker.revoked_at.is_(None))
                .order_by(Worker.hostname_label)
            )
        ).all()
    )
    by_model: dict[uuid.UUID, list[ModelWorkerAvailabilityResponse]] = {}
    now = utc_now()
    for availability, worker in rows:
        age = max(0.0, (now - worker.last_heartbeat).total_seconds())
        by_model.setdefault(availability.model_version_id, []).append(
            ModelWorkerAvailabilityResponse(
                worker_id=worker.id,
                worker_label=worker.hostname_label,
                worker_online=age <= settings.worker_stale_seconds,
                available=availability.available,
                loaded=availability.loaded,
                artifact_checksum=availability.artifact_checksum,
                runtime_fingerprint=availability.runtime_fingerprint,
                capability=availability.capability,
                last_seen_at=availability.last_seen_at,
            )
        )
    program = await session.scalar(select(LibraryProgram).order_by(LibraryProgram.created_at))
    inference_settings = effective_inference_settings(program)
    responses: list[ModelVersionResponse] = []
    for model in models:
        payload = ModelVersionResponse.model_validate(model).model_dump()
        payload["availability"] = by_model.get(model.id, [])
        payload["build"] = model_build_details(model.model_id, model.runtime)
        payload["stage"], payload["active_settings"] = active_settings_for_model(
            model.model_id, inference_settings
        )
        responses.append(ModelVersionResponse.model_validate(payload))
    return responses


@router.get("/packages", response_model=list[ModelPackageResponse])
async def list_all_model_packages(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[ModelPackage]:
    return list(
        (
            await session.scalars(
                select(ModelPackage).order_by(ModelPackage.created_at.desc()).limit(500)
            )
        ).all()
    )


@router.post("", response_model=ModelVersionResponse, status_code=status.HTTP_201_CREATED)
async def register_model(
    payload: ModelVersionCreate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> ModelVersion:
    await lock_operation_family(
        session,
        family=(
            "model-registration:"
            f"{payload.model_id}:{payload.version}:{payload.runtime}:{payload.quantization}"
        ),
        actor_id=None,
    )
    existing = await session.scalar(
        select(ModelVersion).where(
            ModelVersion.model_id == payload.model_id,
            ModelVersion.version == payload.version,
            ModelVersion.runtime == payload.runtime,
            ModelVersion.quantization == payload.quantization,
        )
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="model already registered")
    model = ModelVersion(
        **payload.model_dump(),
        benchmark={},
        enabled=False,
    )
    session.add(model)
    await session.flush()
    await append_audit_event(
        session,
        AuditRecord(
            event_type="model.registered",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"model_version_id": str(model.id)},
            correlation_id=correlation_id,
            after={
                "model_id": model.model_id,
                "version": model.version,
                "runtime": model.runtime,
                "checksum": model.artifact_checksum,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="model",
            event_type="model.registered",
            message="A local model definition was registered",
            correlation_id=correlation_id,
            model_id=model.id,
            details={
                "model_key": model.model_id,
                "runtime": model.runtime,
                "state": "disabled",
            },
        ),
    )
    await session.commit()
    await session.refresh(model)
    return model


@router.put("/{model_id}/benchmark", response_model=ModelVersionResponse)
async def record_benchmark(
    model_id: uuid.UUID,
    payload: ModelBenchmarkUpdate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> ModelVersion:
    model = await session.get(ModelVersion, model_id, with_for_update=True)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    if payload.enabled and not model.artifact_checksum:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a verified artifact checksum is required before enabling a model",
        )
    if payload.enabled:
        required_fields = {
            "dataset",
            "host_profile",
            "median_duration_ms",
            "peak_memory_gib",
            "sample_groups",
            "structured_success_rate",
        }
        if not required_fields.issubset(payload.benchmark):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a complete local benchmark is required before enabling a model",
            )
        try:
            benchmark_valid = (
                isinstance(payload.benchmark["sample_groups"], int)
                and payload.benchmark["sample_groups"] >= 10
                and isinstance(payload.benchmark["structured_success_rate"], (float, int))
                and 0.0 <= float(payload.benchmark["structured_success_rate"]) <= 1.0
                and float(payload.benchmark["median_duration_ms"]) > 0
                and float(payload.benchmark["peak_memory_gib"]) > 0
                and bool(str(payload.benchmark["dataset"]).strip())
                and bool(str(payload.benchmark["host_profile"]).strip())
            )
        except TypeError, ValueError:
            benchmark_valid = False
        if not benchmark_valid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="model benchmark values are outside the approval policy",
            )
    model.benchmark = payload.benchmark
    model.enabled = payload.enabled
    await append_audit_event(
        session,
        AuditRecord(
            event_type="model.benchmark.recorded",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"model_version_id": str(model.id)},
            correlation_id=correlation_id,
            after={"enabled": model.enabled, "benchmark": model.benchmark},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="model",
            event_type="model.benchmark.recorded",
            message="A private model benchmark was recorded",
            correlation_id=correlation_id,
            model_id=model.id,
            details={
                "model_key": model.model_id,
                "runtime": model.runtime,
                "state": "enabled" if model.enabled else "disabled",
            },
        ),
    )
    await session.commit()
    await session.refresh(model)
    return model


@router.get("/{model_id}/packages", response_model=list[ModelPackageResponse])
async def list_model_packages(
    model_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[ModelPackage]:
    return list(
        (
            await session.scalars(
                select(ModelPackage)
                .where(ModelPackage.model_version_id == model_id)
                .order_by(ModelPackage.created_at.desc())
            )
        ).all()
    )


@router.post(
    "/{model_id}/packages",
    response_model=ModelPackageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_model_package(
    model_id: uuid.UUID,
    payload: ModelPackageCreate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> ModelPackage:
    await lock_operation_family(session, family="model-package-catalog", actor_id=None)
    model = await session.get(ModelVersion, model_id, with_for_update=True)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model not found")
    storage = await get_storage_status(session, settings, force_refresh=True)
    if storage.managed_bytes + payload.size_bytes > storage.hard_limit_bytes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="model package would exceed the managed-storage safety cap",
        )
    try:
        await verify_model_package(root=settings.model_root, payload=payload, model=model)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    existing = await session.scalar(
        select(ModelPackage).where(
            ModelPackage.source_url == str(payload.source_url),
            ModelPackage.source_revision == payload.source_revision,
            ModelPackage.artifact_checksum == payload.artifact_checksum,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="package already exists")
    package = ModelPackage(
        model_version_id=model.id,
        source_url=str(payload.source_url),
        source_revision=payload.source_revision,
        artifact_checksum=payload.artifact_checksum,
        license_id=payload.license_id,
        runtime_format=payload.runtime_format,
        size_bytes=payload.size_bytes,
        relative_path=payload.relative_path,
        state="verified",
        approved_by=principal.user.id,
        verified_at=utc_now(),
        is_pinned=payload.is_pinned,
    )
    session.add(package)
    await session.flush()
    await append_audit_event(
        session,
        AuditRecord(
            event_type="model.package.verified",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"model_version_id": str(model.id), "package_id": str(package.id)},
            correlation_id=correlation_id,
            after={
                "checksum": package.artifact_checksum,
                "size_bytes": package.size_bytes,
                "source_revision": package.source_revision,
            },
        ),
    )
    await session.commit()
    await session.refresh(package)
    return package
