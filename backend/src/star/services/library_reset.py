from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NamedTuple

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import Base, utc_now
from star.db.models import (
    AlbumReference,
    AnalysisGeneration,
    AnalysisNode,
    AnalysisObligation,
    Asset,
    AssetReviewCycle,
    AuditEvent,
    CleanupCampaign,
    CollectionGroup,
    DeepReviewBackfill,
    DeepReviewPromptExperiment,
    EnsembleStudentTrainingRun,
    EnsembleStudentVersion,
    EvaluationReservation,
    ExecutionManifest,
    HighlightComparison,
    HighlightLeagueEntry,
    HumanDecision,
    InferencePerformance,
    InferenceRun,
    LibraryPolicyVersion,
    LibraryProgram,
    LibraryRankingRun,
    MachineWorkBlock,
    MediaStagingSuppression,
    ModelDeployment,
    ModelPackage,
    ModelVersion,
    OperationalEvent,
    OwnerFeedbackClock,
    PerformanceBucket,
    PerformanceSnapshot,
    PersonalRankerVersion,
    PersonReference,
    PreferencePhotoInsight,
    PreferencePromptRun,
    ProcessingBatch,
    Proposal,
    ReviewReanalysisRun,
    ServiceHeartbeat,
    StorageInventory,
    TagReference,
    Task,
    TaskFailureEvidence,
    TasteCalibrationSnapshot,
    Worker,
    WorkerCredential,
    WorkRun,
    WorkRunAsset,
    WorkspacePreset,
    WritebackBatch,
)
from star.domain.enums import LibraryProgramState
from star.services.campaigns import canonical_digest
from star.services.library_program import DEFAULT_PROGRAM_NAME, library_program_policy
from star.services.orchestration import SCHEDULER_LOCK_ID
from star.services.reset_barrier import acquire_reset_runtime_exclusive

CONFIRMATION_PHRASE = "RESET PHOTO LIBRARY"
TOKEN_TTL = timedelta(minutes=10)
ResetScope = Literal["photo_library", "fresh_installation"]
_CACHE_PREFIX = re.compile(r"^[0-9a-f]{2}$")


class ResetTokenClaims(NamedTuple):
    counts: dict[str, int]
    scope: ResetScope
    forget_workers: bool


async def photo_domain_counts(session: AsyncSession) -> dict[str, int]:
    models = {
        "assets": Asset,
        "tasks": Task,
        "task_failure_evidence": TaskFailureEvidence,
        "review_cycles": AssetReviewCycle,
        "analysis_generations": AnalysisGeneration,
        "analysis_nodes": AnalysisNode,
        "analysis_obligations": AnalysisObligation,
        "collection_groups": CollectionGroup,
        "highlight_candidates": HighlightLeagueEntry,
        "highlight_comparisons": HighlightComparison,
        "inference_runs": InferenceRun,
        "proposals": Proposal,
        "decisions": HumanDecision,
        "campaigns": CleanupCampaign,
        "deep_review_backfills": DeepReviewBackfill,
        "prompt_experiments": DeepReviewPromptExperiment,
        "processing_batches": ProcessingBatch,
        "ranking_runs": LibraryRankingRun,
        "writeback_batches": WritebackBatch,
        "people": PersonReference,
        "albums": AlbumReference,
        "tags": TagReference,
        "personal_rankers": PersonalRankerVersion,
        "preference_prompt_runs": PreferencePromptRun,
        "preference_photo_insights": PreferencePhotoInsight,
        "ensemble_students": EnsembleStudentVersion,
        "student_training_runs": EnsembleStudentTrainingRun,
        "reanalysis_runs": ReviewReanalysisRun,
    }
    return {
        name: int(await session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in models.items()
    }


async def fresh_installation_counts(session: AsyncSession) -> dict[str, int]:
    counts = await photo_domain_counts(session)
    models = {
        "audit_events": AuditEvent,
        "operational_events": OperationalEvent,
        "service_heartbeats": ServiceHeartbeat,
        "storage_inventories": StorageInventory,
        "model_versions": ModelVersion,
        "model_packages": ModelPackage,
        "model_deployments": ModelDeployment,
        "workers": Worker,
        "worker_credentials": WorkerCredential,
        "ui_presets": WorkspacePreset,
    }
    counts.update(
        {
            name: int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for name, model in models.items()
        }
    )
    return counts


def create_reset_token(
    settings: Settings,
    *,
    actor_id: uuid.UUID,
    generation: int,
    counts: dict[str, int],
    scope: ResetScope = "photo_library",
    forget_workers: bool = False,
) -> tuple[str, datetime]:
    expires_at = utc_now() + TOKEN_TTL
    payload = {
        "actor_id": str(actor_id),
        "generation": generation,
        "counts": counts,
        "scope": scope,
        "forget_workers": forget_workers,
        "expires_at": expires_at.isoformat(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    signature = hmac.new(
        settings.require_session_secret().encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}", expires_at


def verify_reset_token(
    settings: Settings,
    token: str,
    *,
    actor_id: uuid.UUID,
    generation: int,
) -> ResetTokenClaims:
    try:
        encoded, supplied_signature = token.rsplit(".", 1)
        expected_signature = hmac.new(
            settings.require_session_secret().encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise ValueError("reset confirmation token is invalid")
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode()))
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("reset confirmation token is invalid") from exc
    if payload.get("actor_id") != str(actor_id) or payload.get("generation") != generation:
        raise ValueError("reset confirmation token is stale")
    if expires_at <= utc_now():
        raise ValueError("reset confirmation token expired")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("reset confirmation token has no count snapshot")
    scope = payload.get("scope", "photo_library")
    if scope not in {"photo_library", "fresh_installation"}:
        raise ValueError("reset confirmation token has an invalid scope")
    forget_workers = payload.get("forget_workers", False)
    if not isinstance(forget_workers, bool):
        raise ValueError("reset confirmation token has invalid worker handling")
    if forget_workers and scope != "fresh_installation":
        raise ValueError("reset confirmation token has invalid worker handling")
    return ResetTokenClaims(
        counts={str(key): int(value) for key, value in counts.items()},
        scope=scope,
        forget_workers=forget_workers,
    )


async def acquire_reset_transaction_lock(session: AsyncSession) -> None:
    """Exclude scheduler planning while a destructive reset is committed."""

    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    # Canonical advisory-lock order is reset barrier, then scheduler. Ordinary
    # mutating requests already hold the shared reset barrier before they may
    # wait for the scheduler lock. Taking these in the opposite order here
    # would let reset and an owner action deadlock each other.
    await acquire_reset_runtime_exclusive(session)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": SCHEDULER_LOCK_ID},
    )


def reset_guard_counts(counts: dict[str, int]) -> dict[str, int]:
    """Return the destructive count snapshot that must still match at execute.

    Audit, operational telemetry, heartbeats, and storage samples are allowed
    to advance between preview and confirmation. All photo-domain, model,
    worker, credential, and UI rows are guarded against a stale confirmation.
    """

    high_churn = {
        "audit_events",
        "operational_events",
        "service_heartbeats",
        "storage_inventories",
    }
    return {key: value for key, value in counts.items() if key not in high_churn}


def _clear_normalized_derivative_cache(root: Path) -> tuple[int, int]:
    """Remove only Star's fixed content-addressed derivative tree.

    The cache is recoverable from Immich. Refuse symlinks and unexpected
    directory shapes rather than following anything outside the configured
    artifact root.
    """

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return 0, 0
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise OSError("artifact cache root is not a safe directory")
    object_root = root / "sha256"
    try:
        object_stat = object_root.lstat()
    except FileNotFoundError:
        return 0, 0
    if stat.S_ISLNK(object_stat.st_mode) or not stat.S_ISDIR(object_stat.st_mode):
        raise OSError("artifact cache object root is not a safe directory")

    deleted_files = 0
    deleted_bytes = 0
    with os.scandir(object_root) as prefixes:
        prefix_entries = list(prefixes)
    for prefix in prefix_entries:
        prefix_stat = prefix.stat(follow_symlinks=False)
        if (
            not _CACHE_PREFIX.fullmatch(prefix.name)
            or stat.S_ISLNK(prefix_stat.st_mode)
            or not stat.S_ISDIR(prefix_stat.st_mode)
        ):
            raise OSError("artifact cache contains an unexpected entry")
        with os.scandir(prefix.path) as files:
            file_entries = list(files)
        for entry in file_entries:
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
                raise OSError("artifact cache contains a non-regular object")
            Path(entry.path).unlink()
            deleted_files += 1
            deleted_bytes += int(entry_stat.st_size)
        Path(prefix.path).rmdir()
    object_root.rmdir()
    return deleted_files, deleted_bytes


async def reset_photo_domain(
    session: AsyncSession,
    *,
    settings: Settings,
    program: LibraryProgram,
    scope: ResetScope = "photo_library",
    forget_workers: bool = False,
    actor_id: uuid.UUID | None = None,
) -> tuple[dict[str, int], int, int, LibraryProgram]:
    counts = (
        await fresh_installation_counts(session)
        if scope == "fresh_installation"
        else await photo_domain_counts(session)
    )
    cache_files = 0
    cache_bytes = 0
    next_library_generation = program.library_generation + 1
    # Explicit deletes cover polymorphic target IDs and SET NULL relationships;
    # deleting assets then cascades revisions, runs, metrics, contexts, and mappings.
    await session.execute(update(Asset).values(current_generation_id=None))
    for model in (
        InferencePerformance,
        PerformanceBucket,
        PerformanceSnapshot,
        WorkRunAsset,
        WorkRun,
        EvaluationReservation,
        ExecutionManifest,
        TasteCalibrationSnapshot,
        OwnerFeedbackClock,
        MediaStagingSuppression,
        MachineWorkBlock,
        WritebackBatch,
        DeepReviewBackfill,
        DeepReviewPromptExperiment,
        HighlightComparison,
        HighlightLeagueEntry,
        PreferencePhotoInsight,
        PreferencePromptRun,
        LibraryRankingRun,
        HumanDecision,
        Proposal,
        InferenceRun,
        TaskFailureEvidence,
        Task,
        AnalysisNode,
        AnalysisGeneration,
        ReviewReanalysisRun,
        ProcessingBatch,
        CleanupCampaign,
        CollectionGroup,
        Asset,
        PersonReference,
        AlbumReference,
        TagReference,
        EnsembleStudentTrainingRun,
        EnsembleStudentVersion,
        PersonalRankerVersion,
    ):
        await session.execute(delete(model))
    if scope == "fresh_installation":
        # Clear worker affinity before deleting processing history. Keeping enrolled
        # workers is the practical default; the explicit forget option revokes every
        # machine credential and requires a new enrollment.
        await session.execute(
            update(Worker).values(
                affinity_routing_key=None,
                affinity_model_key=None,
                affinity_processing_batch_id=None,
                affinity_remaining=0,
                affinity_task_count=0,
                affinity_switch_count=0,
                affinity_updated_at=None,
                runtime_status={},
            )
        )
        preserved_tables = {
            "users",
            "sessions",
            "immich_instances",
            "immich_connections",
            "api_credentials",
        }
        if not forget_workers:
            preserved_tables.update({"workers", "worker_credentials"})
        # Domain rows and the generation cycle were cleared explicitly above.
        # Use declaration order for the residual sweep so SQLAlchemy does not
        # attempt to topologically sort the intentional Asset/Generation cycle.
        for table in reversed(tuple(Base.metadata.tables.values())):
            if table.name in preserved_tables:
                continue
            await session.execute(delete(table))

        # Create a genuinely new coordinator program rather than retaining the
        # old row and timestamps as hidden runtime history.
        # Use the model's canonical current defaults. Keeping a second literal
        # copy here caused Fresh installation to revive retired v11/Ministral
        # quick-pass policy after the production pipeline had moved to v13.
        program = LibraryProgram(
            name=DEFAULT_PROGRAM_NAME,
            library_generation=next_library_generation,
        )
        session.add(program)
        await session.flush()
        fresh_policy = LibraryPolicyVersion(
            program_id=program.id,
            version=1,
            policy_digest=canonical_digest(library_program_policy(program)),
            policy=library_program_policy(program),
            created_by=actor_id,
        )
        session.add(fresh_policy)
        await session.flush()
        program.active_policy_version_id = fresh_policy.id
    else:
        program.enabled = False
        program.state = LibraryProgramState.DISABLED
        program.ingest_new_assets = False
        program.requested_asset_count = None
        program.original_requested_asset_count = None
        program.admitted_asset_count = 0
        program.finalized_asset_count = 0
        program.source_cursor = 0
        program.random_seed = None
        program.run_started_at = None
        program.last_planned_at = None
        program.last_error_class = None
        program.library_generation = next_library_generation
        program.revision += 1
    cache_files, cache_bytes = await asyncio.to_thread(
        _clear_normalized_derivative_cache,
        settings.artifact_root,
    )
    return counts, cache_files, cache_bytes, program
