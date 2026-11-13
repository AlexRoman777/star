from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from star.api.routers.workers._shared import (
    DEFAULT_DATA_CLASSES,
    MODEL_RESIDENCY_BATCH_SIZE,
    PREFERENCE_APPLE_TASK_TYPES,
    _asset_preview_response,
    _authenticated_worker,
    _clear_lease,
    _effective_apple_queue_categories,
    _effective_worker_state,
    _leased_task,
    _media_lease_id,
    _medium_route_apple_queue_filter,
    _routes_for_capabilities,
    _task_generation_is_current,
    _worker_compatibility_details,
    _worker_protocol_is_compatible,
    _worker_protocol_value,
    _worker_release_is_compatible,
)
from star.core.config import Settings, get_settings
from star.core.observability import record_gate
from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    Asset,
    LibraryProgram,
    Task,
    Worker,
)
from star.db.session import get_session
from star.domain.enums import LibraryProgramState, TaskState
from star.domain.schemas import (
    MediaStageResultSubmission,
    WorkerModelWarmupSubmission,
    WorkerPromptResponse,
    WorkerTaskHeartbeat,
    WorkerTaskHeartbeatResponse,
    WorkerTaskLeaseRequest,
    WorkerTaskLeaseResponse,
)
from star.services.library_program import paused_library_task_types
from star.services.machine_blocks import next_phase, observe_block, transition_block
from star.services.model_turns import (
    COMPARISON_TASK_TYPE,
    DiscoveryBudget,
    LaneChoice,
    ModelBacklog,
    choose,
    comparison_compute_share,
    discovery_target_share,
    drain_rates,
    fastest_comparison_machine,
    measured_seconds,
    ready_backlogs,
)
from star.services.normalized_derivatives import (
    cached_normalized_derivative,
    store_normalized_derivative,
)
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_budget import (
    APPLE_ADVISORY_COMPUTE_BUDGET_MS,
    PHOTO_COMPUTE_BUDGET_MS,
    PHOTO_COMPUTE_BUDGET_POLICY,
)
from star.services.pipeline_contract import (
    LEASE_TASK_CONTRACTS,
    TASK_RUNTIME_CONTRACTS,
    lease_task_types_for_route,
)
from star.services.review_prompts import (
    effective_prompt_text,
    ensure_prompt_revision_history,
    prompt_revision,
    prompt_text_at_revision,
)
from star.services.review_timing import (
    analysis_generation_runtime_ms,
    analysis_generation_runtime_ms_by_id,
)
from star.services.storage_inventory import StorageAdmissionError
from star.services.task_recovery import (
    reap_expired_task_leases,
)
from star.services.task_scope import task_currency_reason
from star.services.worker_contract import compatible_task_types, task_lease_contract
from star.services.worker_windows import helper_nvidia_assist_active

router = APIRouter(prefix="/workers", tags=["Workers"])
APPLE_RUNTIME_LOCK_ID = 7_061_720_260_002
APPLE_RUNTIME_COOLDOWN_FLOOR_SECONDS = 35
APPLE_NVIDIA_ASSIST_LEASE_MINUTES = 5
APPLE_NVIDIA_ASSIST_PARALLEL_LEASE_MINUTES = 8
_prompt_history_reconciled = False
_prompt_history_lock = asyncio.Lock()


def _lease_duration(
    *,
    worker: Worker,
    route: str,
    settings: Settings,
) -> timedelta:
    """Bound Apple primary leases without shortening authoritative NVIDIA leases."""

    apple_helper = (
        worker.capabilities.get("apple-silicon") is True
        and worker.capabilities.get("small-vlm") is True
    )
    if route == "small-vlm" and apple_helper and helper_nvidia_assist_active(worker):
        raw_lanes = (worker.runtime_status or {}).get("model_lane_count")
        parallel_lanes = raw_lanes if isinstance(raw_lanes, int) else 1
        # Parallel requests stretch per-task wall time under contention; lease
        # expiry churn would otherwise corrupt the parallelism benchmark itself.
        minutes = (
            APPLE_NVIDIA_ASSIST_PARALLEL_LEASE_MINUTES
            if parallel_lanes > 1
            else APPLE_NVIDIA_ASSIST_LEASE_MINUTES
        )
        return timedelta(minutes=minutes)
    return timedelta(minutes=settings.task_lease_minutes)


def _available_apple_peer(
    worker: Worker,
    *,
    capability: str,
    now: datetime,
    settings: Settings,
) -> bool:
    return (
        worker.capabilities.get(capability) is True
        and _effective_worker_state(worker) == "active"
        and worker.state in {"active", "draining"}
        and (now - worker.last_heartbeat).total_seconds() <= settings.worker_stale_seconds
    )


async def _apple_review_block(
    session: AsyncSession,
    *,
    peers: list[Worker],
    now: datetime,
    settings: Settings,
) -> None:
    """Keep the durable Mac block bookkeeping current (one phase: review).

    The block only feeds the Machines dashboard; leasing never rotates phases.
    """

    helper_peers = [
        candidate
        for candidate in peers
        if helper_nvidia_assist_active(candidate)
        and candidate.capabilities.get("apple-silicon") is True
        and _available_apple_peer(
            candidate,
            capability="small-vlm",
            now=now,
            settings=settings,
        )
    ]
    if not peers:
        return None
    if not helper_peers:
        from star.db.models import MachineWorkBlock

        existing = await session.get(
            MachineWorkBlock, str(peers[0].runtime_status.get("machine_group"))
        )
        if existing is not None:
            existing.reason = "rotation_suspended"
        return None
    helper_model_keys = {
        model_key
        for candidate in helper_peers
        for model_key in candidate.runtime_status.get("available_models", [])
        if isinstance(model_key, str)
    }
    review_ready = 0
    if helper_model_keys:
        review_ready = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.routing_key == "small-vlm",
                    Task.target_ref["model_key"].as_string().in_(helper_model_keys),
                    Task.state.in_((TaskState.QUEUED, TaskState.RETRY_WAITING)),
                    Task.available_at <= now,
                )
            )
            or 0
        )
    block = await observe_block(session, peers=peers, now=now)
    phase, reason = next_phase(block, review_ready=review_ready > 0)
    if phase != block.phase and not block.was_busy:
        transition_block(block, phase, reason)
    elif phase == block.phase:
        block.reason = reason
    return None


async def _resident_work_delayed(
    session: AsyncSession,
    *,
    statement: Select[tuple[Task]],
    worker: Worker,
    resident: str,
    routes: list[str],
) -> bool:
    """Whether the resident model still has work that is merely not runnable yet."""

    delayed = statement.where(
        Task.routing_key.in_(routes),
        Task.task_type != "worker.model_warmup",
        Task.target_ref["model_key"].as_string() == resident,
    )
    if worker.affinity_processing_batch_id is not None:
        delayed = delayed.where(Task.processing_batch_id == worker.affinity_processing_batch_id)
    return bool(await session.scalar(select(exists(delayed.subquery()))))


async def _lane_choice(
    session: AsyncSession,
    *,
    worker: Worker,
    statement: Select[tuple[Task]],
    eligible_statement: Select[tuple[Task]],
    routes: list[str],
    now: datetime,
    settings: Settings,
) -> LaneChoice:
    """The automatic turn decision for this poll (see ``services.model_turns``)."""

    resident = (
        worker.affinity_model_key
        if worker.affinity_routing_key in routes and worker.affinity_model_key
        else None
    )
    unfiltered = await ready_backlogs(session, statement, now)
    if not unfiltered:
        return LaneChoice(resident=resident, target=None, reason="nothing_ready")
    others = [
        candidate
        for candidate in (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None), Worker.id != worker.id)
            )
        ).all()
        if candidate.runtime_status.get("machine_group")
        != worker.runtime_status.get("machine_group")
    ]
    seconds = await measured_seconds(session, [worker.id, *(item.id for item in others)], now)
    budget = await _discovery_budget(
        session,
        worker=worker,
        others=others,
        seconds=seconds,
        ready=unfiltered,
        now=now,
        settings=settings,
    )
    detail: dict[str, float | str] = {}
    backlogs = unfiltered
    if budget is not None:
        detail = {**budget.detail, "discovery": budget.reason}
        if not budget.allows:
            # Comparisons are optional: past their measured share, or on the
            # slower machine, they do not count while reviews wait.
            backlogs = await ready_backlogs(
                session, statement.where(Task.task_type != COMPARISON_TASK_TYPE), now
            )
    if not backlogs:
        return LaneChoice(resident=resident, target=None, reason="nothing_ready", detail=detail)
    if resident is not None and resident not in backlogs:
        resident_excluded = backlogs is not unfiltered and resident in unfiltered
        if not resident_excluded and await _resident_work_delayed(
            session, statement=eligible_statement, worker=worker, resident=resident, routes=routes
        ):
            # Only delayed retries remain for the resident model: keeping it
            # avoids a swap now and another when the retry becomes runnable.
            return LaneChoice(
                resident=resident, target=None, reason="resident_work_delayed", detail=detail
            )
    if resident is None or resident not in backlogs:
        # The resident model has no work this poll may count (its queue is
        # empty, or its comparisons are past their discovery share). It
        # yields at once to the model with the largest waiting backlog: a
        # lane must never keep a model whose work does not count while
        # another model's work waits. v255 shipped without this branch and
        # both machines sat on Highlight comparisons for hours while 946
        # primaries waited.
        best = max(
            backlogs.values(),
            key=lambda item: (item.ready, item.oldest_wait_seconds),
        )
        return LaneChoice(
            resident=resident,
            target=best.model_key,
            target_routes=best.routes,
            switching=resident is not None,
            reason="resident_queue_empty",
            detail=detail,
        )
    rates = drain_rates(
        set(backlogs),
        others,
        seconds,
        now=now,
        stale_seconds=float(settings.worker_stale_seconds),
    )
    choice = choose(
        worker=worker,
        resident=resident,
        backlogs=backlogs,
        seconds_me=seconds.get(worker.id, {}),
        other_rates=rates,
        now=now,
    )
    return replace(choice, detail={**choice.detail, **detail})


async def _discovery_budget(
    session: AsyncSession,
    *,
    worker: Worker,
    others: list[Worker],
    seconds: dict[uuid.UUID, dict[str, float]],
    ready: dict[str, ModelBacklog],
    now: datetime,
    settings: Settings,
) -> DiscoveryBudget | None:
    """How much of this lane the five-star discovery may take right now.

    Comparisons fill the five-star tier from the settled fours. They used to
    wait for the whole re-review pass; now they take a share of the fastest
    comparison machine proportional to how far the tier is below its target,
    measured from the performance buckets, and everything when nothing else
    is ready. None when no comparison is ready on this lane's routes.
    """

    from star.services.highlight_league import HIGHLIGHT_MODEL_KEY, five_star_deficit

    if HIGHLIGHT_MODEL_KEY not in ready:
        return None
    places, holders = await five_star_deficit(session)
    return DiscoveryBudget(
        places=places,
        holders=holders,
        target_share=discovery_target_share(places, holders),
        measured_share=await comparison_compute_share(session, worker.id, now),
        fastest=fastest_comparison_machine(
            worker=worker,
            others=others,
            model_key=HIGHLIGHT_MODEL_KEY,
            seconds_by_worker=seconds,
            now=now,
            stale_seconds=float(settings.worker_stale_seconds),
        ),
        other_work_ready=any(key != HIGHLIGHT_MODEL_KEY for key in ready),
    )


async def _apple_runtime_conflicts(
    session: AsyncSession,
    *,
    worker: Worker,
    routes: set[str],
    now: datetime,
    settings: Settings,
    switching_to_senior: bool = False,
) -> str | None:
    """Order a review machine's two model runtimes (primary lanes, Advanced).

    Returns a short refusal code when the requested routes must wait, else
    ``None``. ``switching_to_senior`` is the automatic turn decision for this
    machine: its primary lanes then stop taking new work and the Advanced lane
    waits until they drain, because the two runtimes cannot share memory.
    """

    accelerator_routes = {"small-vlm", "medium-vlm"}
    if not routes.intersection(accelerator_routes):
        return None
    machine_group = worker.runtime_status.get("machine_group")
    if not isinstance(machine_group, str) or not machine_group:
        return None
    if session.get_bind().dialect.name == "postgresql":
        # Serialize the physical-machine decision with the subsequent task lease.
        await session.scalar(select(func.pg_advisory_xact_lock(APPLE_RUNTIME_LOCK_ID)))
    workers = list((await session.scalars(select(Worker).where(Worker.revoked_at.is_(None)))).all())
    peers = [
        candidate
        for candidate in workers
        if candidate.runtime_status.get("machine_group") == machine_group
    ]
    peer_ids = [candidate.id for candidate in peers]
    if not peer_ids:
        return None
    if switching_to_senior:
        if "medium-vlm" not in routes:
            # A primary lane while the machine has decided to switch to the
            # Advanced model: it waits so the lanes can drain. A photo already
            # running in a sibling lane is never a reason to refuse the
            # remaining lanes otherwise: that self-block reduced six parallel
            # lanes to one.
            return "senior_work_ready"
        else:
            # The Advanced lane may only load the 27B once every primary lane
            # on this machine has drained; the two runtimes cannot share memory.
            draining = await session.scalar(
                select(
                    exists().where(
                        Task.leased_to_worker_id.in_(peer_ids),
                        Task.routing_key == "small-vlm",
                        Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                        or_(Task.lease_expires_at.is_(None), Task.lease_expires_at > now),
                    )
                )
            )
            if draining:
                return "model_lane_draining_for_senior"
    await _apple_review_block(session, peers=peers, now=now, settings=settings)
    return None


async def _current_affinity_work_remains(
    session: AsyncSession,
    *,
    eligible_statement: Select[tuple[Task]],
    worker: Worker,
) -> bool:
    """Keep a model resident while its current work is only delayed or locked.

    A retry that is not due yet is still unfinished model work.  Treating an
    empty *ready* queue as a completed model phase causes an avoidable swap and
    then another swap when the retry becomes runnable.  Warm-up tasks do not
    hold residency because they are not photo work.
    """

    affinity_statement = eligible_statement.where(
        Task.routing_key == worker.affinity_routing_key,
        Task.task_type != "worker.model_warmup",
    )
    if worker.affinity_model_key is not None:
        affinity_statement = affinity_statement.where(
            Task.target_ref["model_key"].as_string() == worker.affinity_model_key
        )
    if worker.affinity_processing_batch_id is not None:
        affinity_statement = affinity_statement.where(
            Task.processing_batch_id == worker.affinity_processing_batch_id
        )
    # Page the pending affinity rows so the common one-current-task answer
    # costs one small fetch instead of materializing the whole affinity queue.
    batch_size = 25
    offset = 0
    while True:
        pending = list(
            (
                await session.scalars(
                    affinity_statement.order_by(None)
                    .order_by(Task.available_at, Task.created_at)
                    .limit(batch_size)
                    .offset(offset)
                )
            ).all()
        )
        if not pending:
            return False
        for candidate in pending:
            if await _task_generation_is_current(session, candidate):
                return True
        offset += batch_size


LEASE_REFUSAL_KEY = "lease_refusal"


def _refuse_lease(
    worker: Worker,
    *,
    reason: str,
    routes: list[str] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Answer one poll with healthy idleness and remember why on the worker row.

    The Machines card reads ``runtime_status.lease_refusal`` to explain an idle
    apple; a later successful lease clears it. Heartbeats carry it forward.
    """

    worker.runtime_status = {
        **worker.runtime_status,
        LEASE_REFUSAL_KEY: {
            "reason": reason,
            "route": ",".join(routes) if routes else None,
            "at": utc_now().isoformat(),
        },
    }
    # The worker row keeps only the last refusal, which a sibling lane's
    # success immediately clears. The header lets each lane log its own
    # refusal, so an idle lane can be explained from the worker's log alone.
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={**(headers or {}), "X-Star-Lease-Refusal": reason},
    )


def _clear_lease_refusal(worker: Worker) -> None:
    if LEASE_REFUSAL_KEY in worker.runtime_status:
        worker.runtime_status = {
            key: value for key, value in worker.runtime_status.items() if key != LEASE_REFUSAL_KEY
        }


@router.post("/tasks/lease", response_model=WorkerTaskLeaseResponse)
async def lease_task(
    payload: WorkerTaskLeaseRequest,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkerTaskLeaseResponse | Response:
    auth = await _authenticated_worker(authorization, session, settings)
    worker = await session.scalar(
        select(Worker).where(Worker.id == auth.worker.id).with_for_update()
    )
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    worker.state = _effective_worker_state(worker)
    if worker.state != "active":
        response = _refuse_lease(
            worker,
            reason=(
                "worker_paused"
                if worker.desired_state == "paused"
                else "schedule_inactive"
                if worker.state == "paused"
                else f"worker_{worker.state}"
            ),
        )
        await session.commit()
        return response
    worker_protocol = _worker_protocol_value(worker.runtime_status.get("worker_protocol_version"))
    if not _worker_protocol_is_compatible(
        worker_protocol,
        settings.minimum_worker_protocol_version,
        settings.maximum_worker_protocol_version,
    ):
        worker.runtime_status = {
            **worker.runtime_status,
            "worker_protocol_version": worker_protocol,
            "compatibility_state": "protocol_mismatch",
            "minimum_worker_protocol_version": settings.minimum_worker_protocol_version,
            "maximum_worker_protocol_version": settings.maximum_worker_protocol_version,
        }
        response = _refuse_lease(worker, reason="protocol_mismatch")
        await session.commit()
        return response
    worker_task_types = compatible_task_types(worker.runtime_status.get("task_contract_revisions"))
    compatibility = _worker_compatibility_details(worker.runtime_status, settings)
    worker.runtime_status = {**worker.runtime_status, **compatibility}
    if not worker_task_types:
        response = _refuse_lease(worker, reason="no_compatible_task_contracts")
        await session.commit()
        return response
    if settings.require_matching_worker_release and settings.image_tag != "dev":
        worker_release = worker.runtime_status.get("release_version", "unversioned")
        expected_release = settings.image_tag
        if not _worker_release_is_compatible(worker_release, expected_release):
            worker.runtime_status = {
                **worker.runtime_status,
                "compatibility_state": "release_mismatch",
                "expected_release_version": expected_release,
            }
            response = _refuse_lease(worker, reason="release_mismatch")
            await session.commit()
            return response
    if worker.runtime_status.get("compatibility_state") in {
        "release_mismatch",
        "protocol_mismatch",
    }:
        worker.runtime_status = {
            key: value
            for key, value in worker.runtime_status.items()
            if key
            not in {
                "compatibility_state",
                "expected_release_version",
                "minimum_worker_protocol_version",
                "maximum_worker_protocol_version",
            }
        }
    program = await session.scalar(select(LibraryProgram).limit(1))
    if program is not None and program.state == LibraryProgramState.PAUSED:
        response = _refuse_lease(worker, reason="library_program_paused")
        await session.commit()
        return response
    accepted = set(payload.accepted_capabilities)
    if not accepted.issubset(set(worker.capabilities)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="lease request exceeds worker capabilities",
        )
    now = utc_now()
    await reap_expired_task_leases(session, now=now, worker_id=worker.id)
    advertised_concurrency = worker.runtime_status.get("maximum_concurrency", 1)
    maximum_concurrency = (
        advertised_concurrency
        if isinstance(advertised_concurrency, int) and not isinstance(advertised_concurrency, bool)
        else 1
    )
    maximum_concurrency = min(128, max(1, maximum_concurrency))
    active_leases = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.leased_to_worker_id == worker.id,
                Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at > now,
            )
        )
        or 0
    )
    if active_leases >= maximum_concurrency:
        response = _refuse_lease(worker, reason="worker_at_capacity")
        await session.commit()
        return response
    requested_routes = set(payload.accepted_routing_keys) or _routes_for_capabilities(accepted)
    credential_routes = (
        set(auth.credential.allowed_routing_keys)
        if auth.credential is not None
        else _routes_for_capabilities(set(worker.allowed_capabilities))
    )
    # Pre-v95 technical credentials already authorized similarity evidence but could not
    # name the later, narrower semantic-embedding route. Preserve device identity across
    # upgrades without broadening any unrelated route: the compatibility alias requires
    # both the legacy route grant and the worker's explicit enrolled capability.
    if (
        "similarity" in credential_routes
        and "semantic-embedding" in set(worker.capabilities)
        and "semantic-embedding" in set(worker.allowed_capabilities)
    ):
        credential_routes.add("semantic-embedding")
    routes = sorted(requested_routes.intersection(credential_routes))
    data_classes = set(
        set(auth.credential.allowed_data_classes)
        if auth.credential is not None
        else DEFAULT_DATA_CLASSES
    )
    # Operational metadata (the nightly reading's task) carries no photograph
    # and nothing about one; a credential trusted with metadata-only work may
    # take it. Without this the reading waited for days behind a class no
    # worker credential names.
    if "metadata-only" in data_classes:
        data_classes.add("operational-metadata")
    # v100 technical credentials predate the bounded family-metadata class used
    # by description embeddings. Permit only that narrow class on the existing
    # semantic route when the same enrolled worker is already trusted for family
    # previews; do not broaden other worker routes or credentials.
    if (
        "semantic-embedding" in routes
        and "family-preview" in data_classes
        and "semantic-embedding" in set(worker.capabilities)
        and "semantic-embedding" in set(worker.allowed_capabilities)
    ):
        data_classes.add("family-metadata")
    sorted_data_classes = sorted(data_classes)
    if not routes or not data_classes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="lease request has no routes permitted by the worker credential",
        )
    route_conditions: list[ColumnElement[bool]] = []
    model_routes = {"small-vlm", "medium-vlm"}.intersection(routes)
    for route in routes:
        task_types = lease_task_types_for_route(route).intersection(worker_task_types)
        if route in model_routes:
            task_types.add("worker.model_warmup")
        if not task_types:
            continue
        route_condition: ColumnElement[bool] = and_(
            Task.routing_key == route,
            Task.task_type.in_(task_types),
        )
        if route in model_routes:
            if not payload.accepted_model_keys:
                continue
            route_condition = and_(
                route_condition,
                Task.target_ref["model_key"].as_string().in_(payload.accepted_model_keys),
            )
        route_conditions.append(route_condition)
    if not route_conditions:
        response = _refuse_lease(worker, reason="no_leasable_route", routes=routes)
        await session.commit()
        return response
    eligible_statement = select(Task).where(
        or_(*route_conditions),
        Task.data_class.in_(sorted_data_classes),
        Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING}),
    )
    # An explicit owner-started Mac session owns the unified-memory lane until
    # it settles. Do not let ordinary nvidia-assist work slip in between its
    # leases, reload an 8B model, or delay a retry behind the production queue.
    # This is deliberately scoped to the Apple Silicon accelerator; nvidia
    # production review remains independent.
    if worker.capabilities.get("apple-silicon") is True and set(routes).intersection(
        {"small-vlm", "medium-vlm"}
    ):
        owner_session_active = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.task_type.in_(PREFERENCE_APPLE_TASK_TYPES),
                    Task.state.in_(
                        {
                            TaskState.PLANNED,
                            TaskState.QUEUED,
                            TaskState.RUNNING,
                            TaskState.RETRY_WAITING,
                            TaskState.CANCEL_REQUESTED,
                        }
                    ),
                )
            )
            or 0
        )
        if owner_session_active:
            eligible_statement = eligible_statement.where(
                Task.task_type.in_(PREFERENCE_APPLE_TASK_TYPES)
            )
    paused_task_types = paused_library_task_types(program)
    if paused_task_types:
        eligible_statement = eligible_statement.where(~Task.task_type.in_(paused_task_types))
    connection_scope = (
        list(auth.credential.allowed_connection_ids) if auth.credential is not None else []
    )
    if connection_scope:
        eligible_statement = eligible_statement.where(
            or_(
                Task.connection_id.is_(None),
                Task.connection_id.in_([uuid.UUID(value) for value in connection_scope]),
            )
        )
    if "medium-vlm" in routes:
        apple_review_filters = _effective_apple_queue_categories(worker.apple_review_filters)
        category_match = and_(
            Task.task_type != "worker.model_warmup",
            _medium_route_apple_queue_filter(apple_review_filters),
        )
        eligible_statement = eligible_statement.where(
            or_(Task.task_type == "worker.model_warmup", category_match)
        )
    statement = eligible_statement.where(Task.available_at <= now)
    # The automatic model turn: which model this lane should hold now, from
    # the ready backlog on every route it serves, its own measured task and
    # load times, and what other machines already hold resident.
    choice = await _lane_choice(
        session,
        worker=worker,
        statement=statement,
        eligible_statement=eligible_statement,
        routes=routes,
        now=now,
        settings=settings,
    )
    if choice.target is not None and choice.target_routes:
        narrowed = sorted(set(routes).intersection(choice.target_routes))
        if narrowed:
            routes = narrowed
            statement = statement.where(Task.routing_key.in_(routes))
    runtime_conflict = await _apple_runtime_conflicts(
        session,
        worker=worker,
        routes=set(routes),
        now=now,
        settings=settings,
        switching_to_senior=choice.switching and "medium-vlm" in choice.target_routes,
    )
    if runtime_conflict is not None:
        response = _refuse_lease(
            worker,
            reason=runtime_conflict,
            routes=routes,
            headers={"X-Star-Unload-Idle": "1"},
        )
        await session.commit()
        return response
    effective_minimum_depth = (
        1
        if "medium-vlm" in routes and not helper_nvidia_assist_active(worker)
        else payload.minimum_matching_queue_depth
    )
    if (
        effective_minimum_depth > 1
        and "medium-vlm" in routes
        and "small-vlm" not in routes
        and payload.urgent_deep_review_bypass
    ):
        effective_minimum_depth = 1
    if effective_minimum_depth > 1:
        queue_origin = Task.target_ref["queue_origin"].as_string()
        first_escalation_reason = Task.target_ref["escalation_reasons"][0].as_string()
        urgent_task = None
        if payload.urgent_deep_review_bypass and "medium-vlm" in routes:
            urgent_task = await session.scalar(
                statement.where(
                    or_(
                        queue_origin.in_(["manual", "user"]),
                        first_escalation_reason.in_(["nsfw_privacy_review", "privacy_uncertain"]),
                    )
                ).limit(1)
            )
        if urgent_task is None:
            matching_count = int(
                await session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            if matching_count < effective_minimum_depth:
                response = _refuse_lease(worker, reason="queue_below_minimum_depth", routes=routes)
                await session.commit()
                return response
    completed_evidence_count = (
        select(func.count(AnalysisRun.id))
        .where(
            AnalysisRun.asset_id == Task.asset_id,
            AnalysisRun.asset_revision_id == Task.asset_revision_id,
            AnalysisRun.status == "succeeded",
        )
        .correlate(Task)
        .scalar_subquery()
    )
    prefer_backfill = worker.affinity_task_count % 4 == 3
    queue_origin = Task.target_ref["queue_origin"].as_string()
    origin_order = case(
        (Task.task_type.in_(PREFERENCE_APPLE_TASK_TYPES), 0),
        (queue_origin.in_(["manual", "user", "run_finalization"]), 1),
        (queue_origin == "experiment", 1),
        (queue_origin == "backfill", 2 if prefer_backfill else 3),
        else_=3 if prefer_backfill else 2,
    )
    ordered_statement = statement.order_by(
        origin_order,
        Task.priority,
        completed_evidence_count.desc(),
        Task.available_at,
        Task.created_at,
    )
    # A worker drains every ready task for its resident model before it may lease
    # another model. This is the hard no-overlap boundary for the 8 GB nvidia:
    # finish the cohort's Ministral work, unload it, then begin Qwen3-VL. A timer,
    # an overdue comparison, or newly explicit work must never force a mid-queue
    # model swap. Explicit work for another model is selected as soon as the
    # resident queue is actually empty.
    affinity_eligible = worker.affinity_routing_key in routes and (
        worker.affinity_model_key is None
        or worker.affinity_model_key in payload.accepted_model_keys
    )
    yield_to_other_model = choice.switching and choice.target is not None
    if yield_to_other_model:
        record_gate("model_turn", refused=None)
    if yield_to_other_model or "discovery" in choice.detail:
        # A switch is always recorded; so is any poll that weighed a ready
        # comparison, so the Machines card can say why discovery does or
        # does not count right now.
        worker.runtime_status = {
            **worker.runtime_status,
            "model_turn_decision": {
                "from": choice.resident,
                "to": choice.target,
                "reason": choice.reason,
                **choice.detail,
            },
        }
    for _ in range(32):
        task = None
        if yield_to_other_model:
            task = await session.scalar(
                ordered_statement.where(Task.target_ref["model_key"].as_string() == choice.target)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        if task is None and affinity_eligible:
            affinity_statement = ordered_statement.where(
                Task.routing_key == worker.affinity_routing_key
            )
            if worker.affinity_model_key is not None:
                affinity_statement = affinity_statement.where(
                    Task.target_ref["model_key"].as_string() == worker.affinity_model_key
                )
            if worker.affinity_processing_batch_id is not None:
                affinity_statement = affinity_statement.where(
                    Task.processing_batch_id == worker.affinity_processing_batch_id
                )
            task = await session.scalar(
                affinity_statement.with_for_update(skip_locked=True).limit(1)
            )
            if task is None and await _current_affinity_work_remains(
                session,
                eligible_statement=eligible_statement,
                worker=worker,
            ):
                response = _refuse_lease(
                    worker, reason="resident_model_work_pending", routes=routes
                )
                await session.commit()
                return response
        if task is None:
            task = await session.scalar(
                ordered_statement.where(queue_origin.in_(["manual", "user", "run_finalization"]))
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        if task is None and affinity_eligible and worker.affinity_model_key is not None:
            # A completed internal runway is the safe model-switch boundary. Prefer
            # already-materialized work for the other model before extending the
            # resident model into a newly admitted cohort.
            task = await session.scalar(
                ordered_statement.where(
                    or_(
                        Task.routing_key != worker.affinity_routing_key,
                        Task.target_ref["model_key"].as_string() != worker.affinity_model_key,
                    )
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        if task is None:
            task = await session.scalar(
                ordered_statement.with_for_update(skip_locked=True).limit(1)
            )
        if task is None:
            response = _refuse_lease(worker, reason="queue_empty", routes=routes)
            await session.commit()
            return response
        obsolete_reason = await task_currency_reason(session, task)
        if obsolete_reason is None:
            break
        # Named, counted, and kept on the task: a refusal nobody can explain is
        # how the Apple worker sat idle for days behind thousands of these.
        record_gate(f"lease_obsolete:{task.task_type}", refused=obsolete_reason)
        task.state = TaskState.CANCELLED
        task.last_error_class = "ObsoleteLibraryGeneration"
        task.progress = {
            **task.progress,
            "stage": "cancelled",
            "reason": "obsolete_library_generation",
            "obsolete_reason": obsolete_reason,
        }
        await session.flush()
    else:
        response = _refuse_lease(worker, reason="obsolete_generation_cancelled", routes=routes)
        await session.commit()
        return response
    assert task is not None
    if task.task_type == "asset.primary_review":
        used_compute_ms = await analysis_generation_runtime_ms(
            session,
            analysis_generation_id=task.analysis_generation_id,
        )
        remaining_compute_ms = max(0, PHOTO_COMPUTE_BUDGET_MS - used_compute_ms)
        configured_budget = task.target_ref.get("compute_budget_ms")
        stage_default_budget = remaining_compute_ms
        requested_budget = (
            int(configured_budget)
            if isinstance(configured_budget, (int, float))
            and not isinstance(configured_budget, bool)
            and configured_budget > 0
            else stage_default_budget
        )
        effective_budget = min(requested_budget, remaining_compute_ms)
        task.target_ref = {
            **task.target_ref,
            "compute_budget_policy": PHOTO_COMPUTE_BUDGET_POLICY,
            "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
        }
        if effective_budget <= 0:
            task.state = TaskState.FAILED
            task.last_error_class = "PhotoComputeBudgetExceeded"
            task.progress = {
                "stage": "failed",
                "reason": "photo_compute_budget_exhausted_before_lease",
                "successful_compute_ms": used_compute_ms,
                "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
            }
            response = _refuse_lease(worker, reason="photo_budget_exhausted", routes=routes)
            await session.commit()
            return response
        task.target_ref = {
            **task.target_ref,
            "compute_budget_ms": effective_budget,
            "successful_compute_before_lease_ms": used_compute_ms,
            "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
        }
    if task.task_type == "asset.deep_review":
        # The senior review runs after the NVIDIA worker pipeline may already have
        # consumed the photo's cumulative envelope; it keeps its own guard.
        configured_budget = task.target_ref.get("compute_budget_ms")
        requested_budget = (
            int(configured_budget)
            if isinstance(configured_budget, (int, float))
            and not isinstance(configured_budget, bool)
            and configured_budget > 0
            else APPLE_ADVISORY_COMPUTE_BUDGET_MS
        )
        task.target_ref = {
            **task.target_ref,
            "compute_budget_ms": min(requested_budget, APPLE_ADVISORY_COMPUTE_BUDGET_MS),
            "compute_budget_policy": "senior-review-600s-v1",
            "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
        }
    if task.task_type in {"group.highlight_compare", "group.series_select"}:
        raw_candidate_ids = task.target_ref.get("asset_ids", [])
        candidate_ids: list[uuid.UUID] = []
        if isinstance(raw_candidate_ids, list):
            for value in raw_candidate_ids:
                try:
                    candidate_ids.append(uuid.UUID(str(value)))
                except ValueError:
                    candidate_ids = []
                    break
        # Resolve every candidate's charged generation in three bounded
        # queries instead of one asset, task, and runtime query per photo.
        generation_by_candidate: dict[uuid.UUID, uuid.UUID | None] = dict.fromkeys(candidate_ids)
        if candidate_ids:
            asset_rows = await session.execute(
                select(Asset.id, Asset.current_generation_id).where(Asset.id.in_(candidate_ids))
            )
            generation_by_candidate.update(
                {row_id: generation for row_id, generation in asset_rows.tuples()}
            )
        used_by_generation = await analysis_generation_runtime_ms_by_id(
            session,
            analysis_generation_ids=sorted(
                {value for value in generation_by_candidate.values() if value is not None}
            ),
        )
        remaining_by_photo: list[int] = []
        for candidate_id in candidate_ids:
            generation_id = generation_by_candidate[candidate_id]
            used_ms = used_by_generation.get(generation_id, 0) if generation_id else 0
            remaining_by_photo.append(max(0, PHOTO_COMPUTE_BUDGET_MS - used_ms))
        configured_budget = task.target_ref.get("compute_budget_ms")
        requested_budget = (
            int(configured_budget)
            if isinstance(configured_budget, (int, float))
            and not isinstance(configured_budget, bool)
            and configured_budget > 0
            else 0
        )
        effective_budget = (
            min(requested_budget, min(remaining_by_photo) * len(remaining_by_photo))
            if requested_budget and remaining_by_photo
            else 0
        )
        task.target_ref = {
            **task.target_ref,
            "compute_budget_policy": PHOTO_COMPUTE_BUDGET_POLICY,
            "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
        }
        if effective_budget <= 0:
            # One member whose generation spent its whole budget must not
            # fail every sheet it is drawn into: it leaves the rounds like a
            # photo Immich cannot serve, and the sheet fails once with its
            # name so the Failures page can say which photo.
            exhausted = [
                str(candidate_id)
                for candidate_id, remaining in zip(candidate_ids, remaining_by_photo, strict=True)
                if remaining <= 0
            ]
            if task.task_type == "group.highlight_compare":
                from star.services.highlight_league import mark_highlight_source_unavailable

                for candidate in exhausted:
                    # Spent compute never returns, so neither does the entry.
                    await mark_highlight_source_unavailable(
                        session, uuid.UUID(candidate), permanent=True
                    )
            task.state = TaskState.FAILED
            task.last_error_class = "PhotoComputeBudgetExceeded"
            task.progress = {
                "stage": "failed",
                "reason": "group_candidate_compute_budget_exhausted_before_lease",
                "exhausted_asset_ids": exhausted,
            }
            response = _refuse_lease(worker, reason="photo_budget_exhausted", routes=routes)
            await session.commit()
            return response
        task.target_ref = {
            **task.target_ref,
            "compute_budget_ms": effective_budget,
            "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
            "candidate_remaining_compute_ms": min(remaining_by_photo),
        }
    from star.services.execution_manifests import attach_execution_manifest

    try:
        await attach_execution_manifest(session, task, worker)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Execution instructions are being prepared",
            headers={"Retry-After": "10"},
        ) from exc
    if worker.capabilities.get("apple-silicon"):
        from star.db.models import MachineWorkBlock

        block = await session.get(MachineWorkBlock, str(worker.runtime_status.get("machine_group")))
        if block is not None:
            task.target_ref = {**task.target_ref, "machine_block_id": str(block.block_id)}
    _clear_lease_refusal(worker)
    lease_id = uuid.uuid4()
    task.target_ref = {
        **task.target_ref,
        "execution_concurrency": (
            worker.model_concurrency_limit
            if worker.capabilities.get("apple-silicon")
            and task.task_type == "asset.primary_review"
            and task.target_ref.get("model_key") == worker.runtime_status.get("parallel_model_key")
            else 1
        ),
    }
    lease_duration = _lease_duration(worker=worker, route=task.routing_key, settings=settings)
    expires_at = now + lease_duration
    from star.services.task_deadlines import task_wall_seconds

    wall_seconds = task_wall_seconds(task.task_type)
    if wall_seconds is not None:
        expires_at = min(expires_at, now + timedelta(seconds=wall_seconds + 30))
    task.state = TaskState.RUNNING
    task.leased_to_worker_id = worker.id
    task.lease_id = lease_id
    task.lease_expires_at = expires_at
    task.attempt_count += 1
    target_model_key = task.target_ref.get("model_key")
    model_key = target_model_key if isinstance(target_model_key, str) else None
    same_affinity = (
        worker.affinity_routing_key == task.routing_key and worker.affinity_model_key == model_key
    )
    if same_affinity:
        worker.affinity_remaining = max(0, worker.affinity_remaining - 1)
        worker.affinity_processing_batch_id = task.processing_batch_id
    else:
        if worker.affinity_model_key is not None and worker.affinity_model_key != model_key:
            worker.affinity_switch_count += 1
        if worker.affinity_model_key != model_key:
            # A new turn begins; its length follows the measured switch cost.
            worker.runtime_status = {
                **worker.runtime_status,
                "model_turn_started_at": utc_now().isoformat(),
            }
        worker.affinity_routing_key = task.routing_key
        worker.affinity_model_key = model_key
        worker.affinity_processing_batch_id = task.processing_batch_id
        worker.affinity_remaining = MODEL_RESIDENCY_BATCH_SIZE - 1
    worker.affinity_task_count += 1
    if not same_affinity:
        worker.affinity_updated_at = now
    task.progress = {
        **dict(task.progress or {}),
        "stage": "leased",
        "lease_started_at": now.isoformat(),
        "worker_id": str(worker.id),
        "execution_identity": {
            "task_id": str(task.id),
            "lease_id": str(lease_id),
            "library_generation": task.target_ref.get("library_generation"),
            "program_revision": program.revision if program is not None else None,
            "worker_release": worker.runtime_status.get("release_version"),
            "runtime_session_id": worker.runtime_status.get("runtime_session_id"),
            "model_key": model_key,
            "prompt_version": task.target_ref.get("prompt_version"),
            "apple_nvidia_assist": (
                task.routing_key == "small-vlm"
                and worker.capabilities.get("apple-silicon") is True
                and helper_nvidia_assist_active(worker)
            ),
        },
        "coordinator": {
            "strategy": "completion-first-model-residency-fair-backfill",
            "model_key": model_key,
            "processing_batch_id": (
                str(task.processing_batch_id) if task.processing_batch_id is not None else None
            ),
            "remaining_in_residency_batch": worker.affinity_remaining,
        },
    }
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="task.leased",
            message="Worker acquired a task lease",
            task_id=task.id,
            worker_id=worker.id,
            connection_id=task.connection_id,
            details={
                "task_type": task.task_type,
                "routing_key": task.routing_key,
                "attempt": task.attempt_count,
                "model_key": model_key,
                "processing_batch_id": (
                    str(task.processing_batch_id) if task.processing_batch_id is not None else None
                ),
                "residency_batch_remaining": worker.affinity_remaining,
                "coordinator_strategy": "completion-first-model-residency-fair-backfill",
            },
        ),
    )
    await session.commit()
    return WorkerTaskLeaseResponse(
        task_id=task.id,
        lease_id=lease_id,
        task_type=task.task_type,
        routing_key=task.routing_key,
        data_class=task.data_class,
        target_ref=task.target_ref,
        requirements={
            **task.requirements,
            "worker_contract": task_lease_contract(task.task_type),
        },
        checkpoint=dict(task.checkpoint or {}),
        attempt=task.attempt_count,
        lease_expires_at=expires_at,
    )


@router.post("/tasks/{task_id}/complete-warmup", status_code=status.HTTP_204_NO_CONTENT)
async def complete_model_warmup_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: WorkerModelWarmupSubmission,
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
    if task.task_type != "worker.model_warmup":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task does not accept a model warm-up result",
        )
    task.state = TaskState.SUCCEEDED
    task.progress = {
        "stage": "completed",
        "model_key": task.target_ref.get("model_key"),
        "model_reported_version": payload.model_reported_version,
        "runtime": payload.runtime,
        "duration_ms": payload.duration_ms,
    }
    _clear_lease(task)
    await append_operation_event(
        session,
        OperationRecord(
            source="model",
            event_type="model.warmup.completed",
            message="Local model loaded and ready for photo review",
            task_id=task.id,
            worker_id=auth.worker.id,
            details={
                "model_key": task.target_ref.get("model_key"),
                "runtime": payload.runtime,
                "duration_ms": payload.duration_ms,
            },
        ),
    )
    await session.commit()


@router.post("/tasks/{task_id}/complete-media-stage", status_code=status.HTTP_204_NO_CONTENT)
async def complete_media_stage_task(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: MediaStageResultSubmission,
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
    if task.task_type != "asset.media_stage":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task does not accept a media staging result",
        )
    task.state = TaskState.SUCCEEDED
    task.progress = {
        "stage": "completed",
        "variants_stored": list(payload.variants_stored),
        "bytes_stored": payload.bytes_stored,
        "duration_ms": payload.duration_ms,
    }
    _clear_lease(task)
    await session.commit()


@router.post("/tasks/{task_id}/heartbeat", response_model=WorkerTaskHeartbeatResponse)
async def heartbeat_task_lease(
    task_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: WorkerTaskHeartbeat | None = None,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkerTaskHeartbeatResponse:
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=lease_id,
        allow_cancel_requested=True,
    )
    cancel_requested = task.state == TaskState.CANCEL_REQUESTED
    if payload is not None:
        lease_started_at = task.progress.get("lease_started_at")
        task.progress = {**task.progress, **payload.progress}
        if lease_started_at is not None:
            task.progress = {**task.progress, "lease_started_at": lease_started_at}
        # Ordinary periodic heartbeats intentionally omit checkpoint.  Only an
        # explicit pass-boundary update may replace durable resumable state.
        if "checkpoint" in payload.model_fields_set and payload.checkpoint:
            task.checkpoint = payload.checkpoint
    task.lease_expires_at = utc_now() + _lease_duration(
        worker=auth.worker,
        route=task.routing_key,
        settings=settings,
    )
    from star.services.task_deadlines import task_wall_seconds

    wall_seconds = task_wall_seconds(task.task_type)
    started = task.progress.get("lease_started_at")
    if wall_seconds is not None and isinstance(started, str):
        try:
            lease_start = datetime.fromisoformat(started)
        except ValueError:
            lease_start = None
        if lease_start is not None and lease_start.tzinfo is not None:
            # Permit one final failure/result submission after the local guard.
            task.lease_expires_at = min(
                task.lease_expires_at, lease_start + timedelta(seconds=wall_seconds + 30)
            )
    if task.routing_key in {"small-vlm", "medium-vlm"}:
        from star.services.machine_blocks import observe_existing_block

        await observe_existing_block(session, auth.worker, now=utc_now())
    await session.commit()
    return WorkerTaskHeartbeatResponse(
        task_id=task.id,
        lease_id=lease_id,
        lease_expires_at=task.lease_expires_at,
        cancel_requested=cancel_requested,
    )


@router.get("/execution-manifests/{digest}")
async def get_execution_manifest(
    digest: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    from star.db.models import ExecutionManifest

    await _authenticated_worker(authorization, session, settings)
    manifest = await session.get(ExecutionManifest, digest)
    if manifest is None:
        raise HTTPException(status_code=404, detail="execution manifest unavailable")
    return {"digest": manifest.digest, "payload": manifest.payload}


@router.get("/prompts/{prompt_key:path}", response_model=WorkerPromptResponse)
async def get_worker_prompt(
    prompt_key: str,
    version: int | None = None,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkerPromptResponse:
    """Return the coordinator-authoritative instruction text for one review prompt.

    Workers keep their built-in prompt as a fallback, so a coordinator that has no
    override still produces the released behaviour.
    """

    await _authenticated_worker(authorization, session, settings)
    global _prompt_history_reconciled
    if not _prompt_history_reconciled:
        async with _prompt_history_lock:
            if not _prompt_history_reconciled:
                await ensure_prompt_revision_history(session, actor_id=None)
                # Persist the one-time baseline reconciliation before caching it
                # in this API process. A failed commit must remain retryable.
                await session.commit()
                _prompt_history_reconciled = True
    instruction = (
        await prompt_text_at_revision(session, prompt_key=prompt_key, version=version)
        if version is not None
        else await effective_prompt_text(session, prompt_key)
    )
    if instruction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown inference prompt"
        )
    revision = version if version is not None else await prompt_revision(session, prompt_key)
    await session.commit()
    return WorkerPromptResponse(
        instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
        prompt_key=prompt_key,
        revision=revision,
        instruction=instruction,
    )


@router.get("/tasks/{task_id}/media")
async def get_task_preview(
    task_id: uuid.UUID,
    lease_id: uuid.UUID | None = None,
    variant: Literal["thumbnail", "fullsize"] | None = None,
    header_lease_id: uuid.UUID | None = Header(default=None, alias="X-Star-Lease"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> Response:
    effective_lease_id = _media_lease_id(lease_id, header_lease_id)
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=effective_lease_id,
    )
    single_media_task_types = {
        task_type for task_type in LEASE_TASK_CONTRACTS if task_type.startswith("asset.")
    }
    if task.task_type not in single_media_task_types or not task.target_ref.get("asset_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="wrong task input route"
        )
    # Only the staging task may choose its derivative size; review inputs stay
    # pinned to the released full-size contract.
    if variant is not None and task.task_type != "asset.media_stage":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task media variant is fixed by the release contract",
        )
    asset_id = uuid.UUID(str(task.target_ref.get("asset_id")))
    return await _asset_preview_response(
        session,
        settings,
        asset_id,
        variant=variant or "fullsize",
        background=task.task_type == "asset.media_stage",
    )


@router.get("/tasks/{task_id}/media/{asset_id}")
async def get_group_task_preview(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    lease_id: uuid.UUID | None = None,
    header_lease_id: uuid.UUID | None = Header(default=None, alias="X-Star-Lease"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> Response:
    effective_lease_id = _media_lease_id(lease_id, header_lease_id)
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=effective_lease_id,
    )
    allowed_assets = {str(value) for value in task.target_ref.get("asset_ids", [])}
    group_media_task_types = {
        task_type for task_type in LEASE_TASK_CONTRACTS if task_type.startswith("group.")
    }
    if task.task_type not in group_media_task_types or str(asset_id) not in allowed_assets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="asset is outside the leased model-review task",
        )
    try:
        return await _asset_preview_response(session, settings, asset_id, variant="fullsize")
    except HTTPException as exc:
        # A comparison candidate Immich cannot serve leaves the rounds now,
        # instead of failing every round it is drawn into.
        if (
            exc.status_code == status.HTTP_404_NOT_FOUND
            and task.task_type == "group.highlight_compare"
        ):
            from star.services.highlight_league import mark_highlight_source_unavailable

            if await mark_highlight_source_unavailable(session, asset_id):
                await session.commit()
        raise


_NORMALIZED_VARIANT_EDGES: dict[str, int] = {"thumbnail": 720, "preview": 2048}


def _task_normalization_edge(task: Task) -> int:
    """Return the longest edge the leased task normalizes its media to."""

    value = task.target_ref.get("max_media_edge")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    contract = TASK_RUNTIME_CONTRACTS.get(task.task_type)
    if contract is not None and contract.max_media_edge is not None:
        return contract.max_media_edge
    return _NORMALIZED_VARIANT_EDGES["preview"]


@router.post("/tasks/{task_id}/normalized-media/{asset_id}", status_code=204)
async def store_task_normalized_media(
    task_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    lease_id: uuid.UUID | None = None,
    variant: Literal["thumbnail", "preview"] | None = None,
    header_lease_id: uuid.UUID | None = Header(default=None, alias="X-Star-Lease"),
    encoding_quality: int | None = Header(
        default=None,
        alias="X-Star-Encoding-Quality",
    ),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Persist one clean worker-normalized JPEG for model and browser reuse."""

    effective_lease_id = _media_lease_id(lease_id, header_lease_id)
    auth = await _authenticated_worker(authorization, session, settings)
    task = await _leased_task(
        session,
        worker=auth.worker,
        task_id=task_id,
        lease_id=effective_lease_id,
    )
    if task.task_type.startswith("group."):
        allowed_assets = {str(value) for value in task.target_ref.get("asset_ids", [])}
        allowed = str(asset_id) in allowed_assets
    else:
        allowed = str(task.target_ref.get("asset_id")) == str(asset_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="asset is outside the leased normalization task",
        )
    effective_variant: Literal["thumbnail", "preview"] = variant or "preview"
    # The shared cache holds one full-contract derivative per variant. A task
    # that normalizes to a smaller envelope (the 1440-pixel senior review, a
    # benchmark at 1024 pixels) must not replace the 2048-pixel preview the
    # Photos views and every other review reuse; only a full-edge upload may
    # populate the cache.
    if _task_normalization_edge(task) != _NORMALIZED_VARIANT_EDGES[effective_variant]:
        await session.commit()
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"X-Star-Cache": "skipped"},
        )
    if request.headers.get("content-type", "").split(";", 1)[0] != "image/jpeg":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="normalized media must be JPEG",
        )
    if encoding_quality != 80:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="normalized media must use JPEG quality 80",
        )
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="normalized media content length is invalid",
            ) from exc
        if not 0 < content_length <= 16 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="normalized media exceeds the 16 MiB cache limit",
            )
    # Cache persistence is optional evidence reuse, not task completion. Release
    # the leased task row before bounded disk I/O so heartbeat and result paths
    # cannot queue behind a cache write.
    await session.commit()
    content = await request.body()
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
    if asset is None or asset.current_revision_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    # An intact derivative for the current revision is retained as-is: rewriting
    # it with a re-encoded upload would change the content digest and orphan the
    # previously mapped file for no evidence gain.
    existing = await cached_normalized_derivative(
        session,
        settings,
        asset=asset,
        variant=effective_variant,
    )
    if existing is not None:
        await session.commit()
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"X-Star-Cache": "retained"},
        )
    try:
        await store_normalized_derivative(
            session,
            settings,
            asset=asset,
            variant=effective_variant,
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StorageAdmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="normalized media cache is temporarily full",
        ) from exc
    except OSError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="normalized media cache is temporarily unavailable",
        ) from exc
    await session.commit()
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Star-Cache": "stored"},
    )
