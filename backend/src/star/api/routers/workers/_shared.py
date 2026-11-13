from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import and_, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from star.core.config import Settings
from star.core.crypto import constant_time_equal, hash_session_token
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    Task,
    Worker,
    WorkerCredential,
)
from star.domain.enums import TaskState
from star.domain.schemas import (
    WorkerHeartbeat,
    WorkerResponse,
    WorkerSchedulePolicy,
    WorkerWorkloadResponse,
)
from star.services.media_relay import (
    DERIVATIVE_ROLES,
    MEDIA_CONTRACT_VERSION,
    DerivativeSize,
    MediaRelayError,
    media_relay_http_status,
    open_asset_media_response,
)
from star.services.network_trust import trusted_private_address
from star.services.normalized_derivatives import cached_normalized_derivative
from star.services.pipeline_contract import (
    RETIRED_TASK_DETAIL,
    RETIRED_TASK_TYPES,
    lease_task_types_for_route,
    routes_for_capabilities,
)
from star.services.reset_barrier import acquire_reset_runtime_shared
from star.services.visual_revision import current_visual_revision
from star.services.worker_contract import (
    WORKER_CONTRACT_HASH,
    compatible_task_types,
    task_contract_gaps,
)
from star.services.worker_windows import (
    schedule_allows_work,
)

AUTOMATIC_APPLE_QUEUE_CATEGORIES = frozenset(
    {"manual", "sensitive", "five_star", "four_star", "series", "highlight"}
)
PREFERENCE_APPLE_TASK_TYPES = frozenset({"group.series_select"})


def _effective_apple_queue_categories(categories: list[str]) -> set[str]:
    if "automatic" in categories:
        return set(AUTOMATIC_APPLE_QUEUE_CATEGORIES)
    normalized = set(categories) & set(AUTOMATIC_APPLE_QUEUE_CATEGORIES)
    return normalized if normalized else set(AUTOMATIC_APPLE_QUEUE_CATEGORIES)


def _apple_queue_category(target_ref: object) -> str | None:
    if not isinstance(target_ref, dict):
        return None
    if target_ref.get("type") == "worker.model_warmup":
        return None
    category = target_ref.get("apple_queue_category")
    if isinstance(category, str):
        return category
    queue_origin = target_ref.get("queue_origin")
    if queue_origin is None:
        queue_origin = "live"
    if queue_origin in {"manual", "user"}:
        return "manual"
    if queue_origin == "similarity":
        return "series"
    if queue_origin == "highlight_league":
        return "highlight"
    if queue_origin in {"backfill", "live", "run_finalization"}:
        return "four_star"
    return None


def _medium_route_apple_queue_filter(filters: set[str]) -> ColumnElement[bool]:
    task_category = Task.target_ref["apple_queue_category"].as_string()
    queue_origin = Task.target_ref["queue_origin"].as_string()
    categories_ok = task_category.in_(filters)
    legacy = task_category.is_(None)
    manual_enabled = true() if "manual" in filters else false()
    series_enabled = true() if "series" in filters else false()
    four_star_enabled = true() if "four_star" in filters else false()
    highlight_enabled = true() if "highlight" in filters else false()
    return or_(
        categories_ok,
        and_(legacy, queue_origin.in_({"manual", "user"}), manual_enabled),
        and_(legacy, queue_origin == "similarity", series_enabled),
        and_(
            legacy,
            queue_origin.in_({"backfill", "live", "run_finalization"}),
            four_star_enabled,
        ),
        and_(legacy, queue_origin.is_(None), four_star_enabled),
        and_(
            legacy,
            queue_origin == "highlight_league",
            highlight_enabled,
        ),
    )


DEFAULT_DATA_CLASSES = {"metadata-only", "family-metadata", "family-preview"}


MODEL_RESIDENCY_BATCH_SIZE = 4


# Retained import surface: existing tests and routers use the private name.
_schedule_allows_work = schedule_allows_work


def _validated_helper_schedule(value: dict[str, object] | None) -> WorkerSchedulePolicy | None:
    if value is None:
        return None
    try:
        return WorkerSchedulePolicy.model_validate(value)
    except ValueError:
        # The evaluator fails closed on the same malformed policy; the response
        # simply hides it instead of failing the whole worker listing.
        return None


def _effective_worker_state(worker: Worker) -> str:
    if worker.revoked_at is not None:
        return "revoked"
    if worker.desired_state == "paused":
        return "paused"
    if not _schedule_allows_work(worker.schedule_policy):
        return "paused"
    return "active"


def _worker_release_is_compatible(worker_release: object, expected_release: str) -> bool:
    expected = expected_release.strip()
    if expected in {"", "dev", "unversioned"}:
        return True
    return isinstance(worker_release, str) and worker_release.strip() == expected


def _worker_protocol_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 1 <= value <= 100 else 0
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if 1 <= parsed <= 100 else 0
    return 0


def _worker_protocol_is_compatible(
    worker_protocol: object,
    minimum_protocol: int,
    maximum_protocol: int,
) -> bool:
    version = _worker_protocol_value(worker_protocol)
    return minimum_protocol <= version <= maximum_protocol


def _worker_contract_task_types(runtime_status: dict[str, object]) -> set[str]:
    routes = runtime_status.get("accepted_queues")
    if not isinstance(routes, list):
        return set()
    return {
        task_type
        for route in routes
        if isinstance(route, str)
        for task_type in lease_task_types_for_route(route)
    }


def _worker_compatibility_details(
    runtime_status: dict[str, object], settings: Settings
) -> dict[str, object]:
    protocol = _worker_protocol_value(runtime_status.get("worker_protocol_version"))
    expected_task_types = _worker_contract_task_types(runtime_status)
    missing, outdated = task_contract_gaps(
        runtime_status.get("task_contract_revisions"), task_types=expected_task_types
    )
    if not _worker_protocol_is_compatible(
        protocol,
        settings.minimum_worker_protocol_version,
        settings.maximum_worker_protocol_version,
    ):
        state = "protocol_mismatch"
        summary = (
            f"Worker protocol {protocol or 'unknown'} is outside the supported "
            f"{settings.minimum_worker_protocol_version}-"
            f"{settings.maximum_worker_protocol_version} range."
        )
    elif missing or outdated:
        state = "contract_mismatch"
        count = len(missing) + len(outdated)
        suffix = "s" if count != 1 else ""
        summary = f"Update required for {count} executable task contract{suffix}."
    else:
        state = "compatible"
        summary = "Ready for every task advertised by this worker."
    return {
        "compatibility_state": state,
        "compatibility_summary": summary,
        "worker_protocol_version": protocol,
        "minimum_worker_protocol_version": settings.minimum_worker_protocol_version,
        "maximum_worker_protocol_version": settings.maximum_worker_protocol_version,
        "worker_contract_hash": runtime_status.get("worker_contract_hash", "unknown"),
        "expected_worker_contract_hash": WORKER_CONTRACT_HASH,
        "missing_task_contracts": missing,
        "outdated_task_contracts": outdated,
        "compatible_task_count": len(
            compatible_task_types(runtime_status.get("task_contract_revisions"))
            & expected_task_types
        ),
        "expected_task_count": len(expected_task_types),
    }


def _worker_compatibility_state(runtime_status: dict[str, object], settings: Settings) -> str:
    return str(_worker_compatibility_details(runtime_status, settings)["compatibility_state"])


def _routes_for_capabilities(capabilities: set[str]) -> set[str]:
    routes = routes_for_capabilities(capabilities)
    if capabilities.intersection({"metadata", "cpu-light", "sync", "scheduler"}):
        routes.add("metadata")
    if capabilities.intersection({"image-quality", "technical", "cpu-light"}):
        routes.update({"technical", "similarity"})
    if "model-install" in capabilities:
        routes.add("model-install")
    return routes


def _runtime_status(payload: WorkerHeartbeat) -> dict[str, object]:
    return {
        "release_version": payload.release_version,
        "build_git_sha": payload.build_git_sha,
        "build_tree_state": payload.build_tree_state,
        "build_source_fingerprint": payload.build_source_fingerprint,
        "machine_group": payload.machine_group,
        "worker_protocol_version": payload.worker_protocol_version,
        "worker_contract_hash": payload.worker_contract_hash,
        "task_contract_revisions": payload.task_contract_revisions,
        "current_load": payload.current_load,
        "host_current_load": payload.host_current_load,
        "free_storage_bytes": payload.free_storage_bytes,
        "temperature_c": payload.temperature_c,
        "power_state": payload.power_state,
        "battery_percent": payload.battery_percent,
        "battery_charging": payload.battery_charging,
        "battery_charger_wattage": payload.battery_charger_wattage,
        "battery_admission_state": payload.battery_admission_state,
        "battery_pause_at_percent": payload.battery_pause_at_percent,
        "battery_resume_at_percent": payload.battery_resume_at_percent,
        "battery_heavy_task_cooldown_seconds": payload.battery_heavy_task_cooldown_seconds,
        "network_status": payload.network_status,
        "available_models": payload.available_models,
        "accepted_queues": payload.accepted_queues,
        "maximum_concurrency": payload.maximum_concurrency,
        "model_lane_count": payload.model_lane_count,
        "parallel_model_key": payload.parallel_model_key,
        "model_load_context_length": payload.model_load_context_length,
        "model_load_parallel": payload.model_load_parallel,
        "model_kv_cache_type": payload.model_kv_cache_type,
        "resident_model_keys": payload.resident_model_keys,
        "model_idle_ttl_seconds": payload.model_idle_ttl_seconds,
        "runtime_phase": payload.runtime_phase,
        "runtime_model_key": payload.runtime_model_key,
        "runtime_phase_started_at": payload.runtime_phase_started_at,
        "runtime_model_loaded_at": payload.runtime_model_loaded_at,
        "quiet_mode_active": payload.quiet_mode_active,
        "quiet_mode_start": payload.quiet_mode_start,
        "quiet_mode_end": payload.quiet_mode_end,
        "quiet_mode_timezone": payload.quiet_mode_timezone,
        "quiet_mode_cooldown_seconds": payload.quiet_mode_cooldown_seconds,
        "quiet_mode_performance_percent": payload.quiet_mode_performance_percent,
        "runtime_session_id": (
            str(payload.runtime_session_id) if payload.runtime_session_id is not None else None
        ),
        "runtime_session_started_at": (
            payload.runtime_session_started_at.isoformat()
            if payload.runtime_session_started_at is not None
            else None
        ),
    }


def _expected_worker_release(settings: Settings) -> str:
    return settings.image_tag


def _worker_profile(capabilities: set[str]) -> tuple[str, list[str], str]:
    focus: list[str] = []
    roles: list[str] = []
    if capabilities.intersection({"technical", "image-quality", "cpu-light"}):
        roles.append("technical screening")
        focus.extend(["focus and blur", "exposure and clipping", "artifacts and resolution"])
    if "apple-vision" in capabilities:
        roles.append("Apple Vision evidence")
        focus.extend(["aesthetics", "scene and subject cues", "faces and saliency"])
    if {"small-vlm", "medium-vlm"}.issubset(capabilities):
        roles.append("primary and Advanced review")
        focus.extend(["primary reviews", "Advanced reviews", "model turns"])
        if "apple-silicon" in capabilities:
            focus.append("optional NVIDIA help")
    elif capabilities.intersection({"small-vlm", "medium-vlm", "large-vlm", "mlx"}):
        roles.append("vision critique")
        focus.extend(["composition and subject", "story and family value", "recoverability"])
    if capabilities.intersection({"metadata", "sync", "scheduler"}):
        roles.append("library coordination")
        focus.extend(["metadata synchronization", "queue planning"])
    summary = ", ".join(dict.fromkeys(roles)) or "general compute"
    return (
        summary.capitalize(),
        list(dict.fromkeys(focus)),
        (
            "Produces evidence and proposals only. It cannot write ratings, trash media, or "
            "override a human decision."
        ),
    )


def _empty_workload(worker: Worker) -> WorkerWorkloadResponse:
    capacity_value = worker.runtime_status.get("maximum_concurrency", 1)
    capacity = (
        capacity_value
        if isinstance(capacity_value, int) and not isinstance(capacity_value, bool)
        else 1
    )
    advertised_model_lanes = worker.runtime_status.get("model_lane_count", 0)
    if isinstance(advertised_model_lanes, int) and not isinstance(advertised_model_lanes, bool):
        selected_model_lanes = min(advertised_model_lanes, worker.model_concurrency_limit)
        capacity -= max(0, advertised_model_lanes - selected_model_lanes)
    return WorkerWorkloadResponse(
        active_count=0,
        capacity=max(1, capacity),
        utilization=0,
        compatible_queue_depth=0,
    )


def _worker_response(
    worker: Worker,
    settings: Settings,
    workload: WorkerWorkloadResponse | None = None,
) -> WorkerResponse:
    last_heartbeat = (
        worker.last_heartbeat
        if worker.last_heartbeat.tzinfo
        else worker.last_heartbeat.replace(tzinfo=UTC)
    )
    heartbeat_age_seconds = max(0, int((utc_now() - last_heartbeat).total_seconds()))
    state = _effective_worker_state(worker)
    online = worker.revoked_at is None and heartbeat_age_seconds <= settings.worker_stale_seconds
    role_summary, focus_areas, decision_scope = _worker_profile(set(worker.capabilities))
    release_version_value = worker.runtime_status.get("release_version", "unversioned")
    release_version = (
        release_version_value.strip()
        if isinstance(release_version_value, str) and release_version_value.strip()
        else "unversioned"
    )
    expected_release_version = _expected_worker_release(settings)
    compatibility = _worker_compatibility_details(worker.runtime_status, settings)
    runtime_status = {
        **worker.runtime_status,
        **compatibility,
    }
    missing_task_contracts = compatibility["missing_task_contracts"]
    outdated_task_contracts = compatibility["outdated_task_contracts"]
    return WorkerResponse(
        id=worker.id,
        hostname_label=worker.hostname_label,
        architecture=worker.architecture,
        operating_system=worker.operating_system,
        software_version=worker.software_version,
        release_version=release_version,
        expected_release_version=expected_release_version,
        update_available=compatibility["compatibility_state"] != "compatible",
        compatibility_state=compatibility["compatibility_state"],
        compatibility_summary=str(compatibility["compatibility_summary"]),
        missing_task_contracts=(
            [str(value) for value in missing_task_contracts]
            if isinstance(missing_task_contracts, list)
            else []
        ),
        outdated_task_contracts=(
            [str(value) for value in outdated_task_contracts]
            if isinstance(outdated_task_contracts, list)
            else []
        ),
        capabilities=worker.capabilities,
        runtime_status=runtime_status,
        state=state,
        desired_state=worker.desired_state,
        apple_review_filters=list(worker.apple_review_filters),
        schedule_policy=WorkerSchedulePolicy.model_validate(worker.schedule_policy),
        schedule_active=_schedule_allows_work(worker.schedule_policy),
        control_revision=worker.control_revision,
        model_concurrency_limit=min(
            worker.model_concurrency_limit,
            max(1, int(worker.runtime_status.get("model_lane_count") or 1)),
        ),
        online=online,
        last_heartbeat=worker.last_heartbeat,
        heartbeat_age_seconds=heartbeat_age_seconds,
        role_summary=role_summary,
        focus_areas=focus_areas,
        decision_scope=decision_scope,
        affinity_routing_key=worker.affinity_routing_key,
        affinity_model_key=worker.affinity_model_key,
        affinity_processing_batch_id=worker.affinity_processing_batch_id,
        affinity_remaining=worker.affinity_remaining,
        affinity_task_count=worker.affinity_task_count,
        affinity_switch_count=worker.affinity_switch_count,
        affinity_updated_at=worker.affinity_updated_at,
        workload=workload or _empty_workload(worker),
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker bearer credential is required",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker bearer credential is required",
        )
    return token


def _automatic_enrollment_source(
    request: Request,
    *,
    edge_marker: str | None,
    forwarded_client_ip: str | None,
    settings: Settings,
) -> str:
    try:
        return trusted_private_address(
            client_host=request.client.host if request.client else None,
            edge_marker=edge_marker,
            forwarded_client_ip=forwarded_client_ip,
            require_edge=settings.worker_auto_enroll_require_edge,
            allowed_cidrs=settings.worker_auto_enroll_cidrs,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="automatic worker enrollment is limited to the trusted private edge",
        ) from exc


@dataclass(frozen=True)
class AuthenticatedWorker:
    worker: Worker
    credential: WorkerCredential | None


async def _authenticated_worker(
    authorization: str | None,
    session: AsyncSession,
    settings: Settings,
) -> AuthenticatedWorker:
    # Reset takes the same advisory lock exclusively. Acquire it before reading
    # or mutating worker identity so a fresh reset cannot interleave with a
    # heartbeat, lease, media read, or result submission.
    await acquire_reset_runtime_shared(session)
    raw_token = _bearer_token(authorization)
    token_hash = hash_session_token(raw_token, settings.require_session_secret())
    credential = await session.scalar(
        select(WorkerCredential).where(WorkerCredential.token_hash == token_hash)
    )
    now = utc_now()
    worker: Worker | None = None
    if credential is not None:
        expires_at = _aware(credential.expires_at)
        grace_expires_at = _aware(credential.grace_expires_at)
        effective_expiry = (
            min(expires_at, grace_expires_at)
            if expires_at is not None and grace_expires_at is not None
            else expires_at
        )
        if (
            credential.revoked_at is None
            and effective_expiry is not None
            and effective_expiry > now
            and constant_time_equal(credential.token_hash, token_hash)
        ):
            worker = await session.get(Worker, credential.worker_id)
    if worker is None and credential is None:
        # One-release compatibility path for credentials issued before scoped rotation.
        worker = await session.scalar(select(Worker).where(Worker.token_hash == token_hash))
    if worker is None or worker.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    return AuthenticatedWorker(worker=worker, credential=credential)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


async def _leased_task(
    session: AsyncSession,
    *,
    worker: Worker,
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    allow_cancel_requested: bool = False,
    result_digest: str | None = None,
) -> Task:
    if result_digest is not None and re.fullmatch(r"[0-9a-f]{64}", result_digest) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="result digest must be a lowercase SHA-256 value",
        )
    # AnalysisRun and inference rows reference the generation. Acquire a key-
    # share lock before the task row so the scheduler cannot hold the generation
    # for update while waiting for this task, which is the inverse order that
    # produced description-embedding deadlocks under concurrent publication.
    task_generation_id = await session.scalar(
        select(Task.analysis_generation_id).where(Task.id == task_id)
    )
    if task_generation_id is not None and session.get_bind().dialect.name == "postgresql":
        await session.scalar(
            select(AnalysisGeneration.id)
            .where(AnalysisGeneration.id == task_generation_id)
            .with_for_update(read=True, key_share=True)
        )
    # Serialize completion attempts for one task.  A worker may retry while the
    # first response is still in flight; without this lock both transactions can
    # observe RUNNING and race immutable evidence inserts.
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is not None and task.task_type in RETIRED_TASK_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RETIRED_TASK_DETAIL)
    if task is not None and task.state == TaskState.SUCCEEDED:
        completed_lease_id = (task.target_ref or {}).get("result_submission_lease_id")
        completed_worker_id = (task.target_ref or {}).get("result_submission_worker_id")
        if completed_lease_id == str(lease_id) and completed_worker_id == str(worker.id):
            completed_digest = (task.target_ref or {}).get("result_submission_digest")
            if (
                isinstance(completed_digest, str)
                and result_digest is not None
                and not constant_time_equal(completed_digest, result_digest)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="result replay digest does not match the committed submission",
                )
            # The original transaction committed but its 204 may have been lost.
            # A replay of the same worker lease is an acknowledgement, never a
            # second evidence write.
            raise HTTPException(status_code=status.HTTP_204_NO_CONTENT)
    lease_expires_at = _aware(task.lease_expires_at) if task else None
    if (
        task is None
        or task.state
        not in (
            {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            if allow_cancel_requested
            else {TaskState.RUNNING}
        )
        or task.leased_to_worker_id != worker.id
        or task.lease_id != lease_id
        or lease_expires_at is None
        or lease_expires_at <= utc_now()
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task lease is invalid or expired",
        )
    if not await _task_generation_is_current(session, task):
        task.state = TaskState.CANCELLED
        task.last_error_class = "ObsoleteLibraryGeneration"
        task.progress = {"stage": "cancelled", "reason": "obsolete_library_generation"}
        _clear_lease(task)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task belongs to an obsolete library generation",
        )
    task.target_ref = {
        **dict(task.target_ref or {}),
        "result_submission_lease_id": str(lease_id),
        "result_submission_worker_id": str(worker.id),
        **({"result_submission_digest": result_digest} if result_digest is not None else {}),
    }
    return task


async def _task_generation_is_current(session: AsyncSession, task: Task) -> bool:
    from star.services.task_scope import task_generation_is_current

    return await task_generation_is_current(session, task)


def _clear_lease(task: Task) -> None:
    if task.leased_to_worker_id is not None:
        task.progress = {
            **dict(task.progress or {}),
            "last_worker_id": str(task.leased_to_worker_id),
        }
    if task.state == TaskState.SUCCEEDED and task.lease_id is not None:
        task.progress = {
            **dict(task.progress or {}),
            "completed_lease_id": str(task.lease_id),
            "completed_worker_id": (
                str(task.leased_to_worker_id) if task.leased_to_worker_id is not None else None
            ),
        }
    task.leased_to_worker_id = None
    task.lease_id = None
    task.lease_expires_at = None


async def _cached_worker_media_response(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    variant: DerivativeSize,
) -> Response | None:
    """Serve a staged NVMe derivative so routine review leaves Immich media idle."""

    asset = await session.get(Asset, asset_id)
    if asset is None or asset.access_state != "available":
        return None
    current = await current_visual_revision(session, asset)
    if current is None:
        return None
    cache_variant: Literal["thumbnail", "preview"] = (
        "thumbnail" if variant == "thumbnail" else "preview"
    )
    cached = await cached_normalized_derivative(
        session,
        settings,
        asset=asset,
        variant=cache_variant,
    )
    if cached is None:
        return None
    try:
        content = await asyncio.to_thread(cached.content_path.read_bytes)
    except OSError:
        return None
    if len(content) != cached.size_bytes:
        return None
    await session.commit()
    # The cached file is the already-normalized JPEG. It is labelled with the
    # "preview" input variant, which the worker media contract accepts unchanged;
    # provenance records may therefore describe a cached-normalized derivative.
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Star-Cache": "normalized-jpeg-q80",
            "X-Star-Input-Variant": cache_variant,
            "X-Star-Media-Contract": MEDIA_CONTRACT_VERSION,
            "X-Star-Source-Role": DERIVATIVE_ROLES[cache_variant],
            "X-Star-Preview-Version": current[1],
            "X-Star-Source-Dimensions": f"{cached.width}x{cached.height}",
            "X-Star-Source-Digest": cached.content_digest,
        },
    )


async def _asset_preview_response(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    variant: DerivativeSize,
    background: bool = False,
) -> Response:
    if variant not in {"thumbnail", "fullsize"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="worker media variant is outside the derivative contract",
        )
    cached = await _cached_worker_media_response(
        session,
        settings,
        asset_id,
        variant=variant,
    )
    if cached is not None:
        return cached
    try:
        return await open_asset_media_response(
            session,
            settings,
            asset_id,
            size=variant,
            worker_stream=True,
            background=background,
        )
    except MediaRelayError as exc:
        raise HTTPException(
            status_code=media_relay_http_status(exc),
            detail="Preview capacity is busy; retry shortly"
            if str(exc) == "immich_preview_busy"
            else "Immich preview unavailable",
            headers={
                "X-Star-Media-Error": str(exc),
                **({"Retry-After": "1"} if str(exc) == "immich_preview_busy" else {}),
            },
        ) from exc


def _media_lease_id(
    lease_id: uuid.UUID | None,
    header_lease_id: uuid.UUID | None,
) -> uuid.UUID:
    effective = header_lease_id or lease_id
    if effective is None:
        raise HTTPException(status_code=400, detail="media lease header is required")
    if header_lease_id is not None and lease_id is not None and header_lease_id != lease_id:
        raise HTTPException(status_code=409, detail="media lease identity conflicts")
    return effective
