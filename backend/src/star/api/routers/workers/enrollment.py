from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.api.routers.workers._shared import (
    DEFAULT_DATA_CLASSES,
    _automatic_enrollment_source,
    _aware,
    _routes_for_capabilities,
    _runtime_status,
    _worker_response,
)
from star.core.config import Settings, get_settings
from star.core.crypto import constant_time_equal, hash_session_token
from star.db.base import utc_now
from star.db.models import (
    Worker,
    WorkerCredential,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    WorkerAutomaticEnrollmentRequest,
    WorkerAutomaticEnrollmentStatus,
    WorkerEnrollmentResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.operation_locks import lock_operation_family
from star.services.operations import OperationRecord, append_operation_event
from star.services.reset_barrier import acquire_reset_runtime_shared
from star.services.worker_identity import verify_automatic_enrollment_signature

router = APIRouter(prefix="/workers", tags=["Workers"])


@router.get("/automatic-enrollment", response_model=WorkerAutomaticEnrollmentStatus)
async def automatic_enrollment_status(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    settings: Settings = Depends(get_settings),
) -> WorkerAutomaticEnrollmentStatus:
    return WorkerAutomaticEnrollmentStatus(
        enabled=settings.worker_auto_enroll_enabled,
        credential_rotation_days=settings.worker_credential_rotation_days,
        allowed_data_classes=sorted(DEFAULT_DATA_CLASSES),
    )


@router.post("/automatic-enroll", response_model=WorkerEnrollmentResponse)
async def automatically_enroll_worker(
    payload: WorkerAutomaticEnrollmentRequest,
    request: Request,
    x_star_edge: str | None = Header(default=None, alias="X-Star-Edge"),
    x_star_client_ip: str | None = Header(default=None, alias="X-Star-Client-IP"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerEnrollmentResponse:
    if not settings.worker_auto_enroll_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="automatic worker enrollment is disabled",
        )
    await acquire_reset_runtime_shared(session)
    source_address = _automatic_enrollment_source(
        request,
        edge_marker=x_star_edge,
        forwarded_client_ip=x_star_client_ip,
        settings=settings,
    )
    now = utc_now()
    issued_at = _aware(payload.issued_at)
    if issued_at is None or abs((now - issued_at).total_seconds()) > (
        settings.worker_auto_enroll_max_clock_skew_seconds
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="automatic worker enrollment request is stale",
        )
    # Older agents did not include newer optional heartbeat fields in the
    # signed document. Excluding unset defaults keeps their enrollment
    # signatures valid while new agents explicitly sign release_version.
    heartbeat_document = payload.heartbeat.model_dump(mode="json", exclude_unset=True)
    try:
        device_subject = verify_automatic_enrollment_signature(
            heartbeat=heartbeat_document,
            device_public_key=payload.device_public_key,
            issued_at=issued_at,
            nonce=payload.nonce,
            signature=payload.signature,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    await lock_operation_family(
        session,
        family=f"worker-enrollment:{device_subject}",
        actor_id=None,
    )
    nonce_hash = hashlib.sha256(payload.nonce.encode("utf-8")).hexdigest()
    requested_capabilities = set(payload.heartbeat.capabilities)
    allowed_capabilities = set(settings.worker_auto_enroll_capabilities)
    unsupported = requested_capabilities - allowed_capabilities
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"automatic enrollment does not permit capabilities: {sorted(unsupported)}",
        )
    worker = await session.scalar(
        select(Worker).where(Worker.certificate_subject == device_subject).with_for_update()
    )
    hostname_owner = await session.scalar(
        select(Worker)
        .where(Worker.hostname_label == payload.heartbeat.hostname_label)
        .with_for_update()
    )
    if worker is not None and worker.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this local worker identity was revoked",
        )
    if worker is not None and constant_time_equal(
        worker.last_enrollment_nonce_hash or "",
        nonce_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="automatic enrollment request was already used",
        )
    if hostname_owner is not None and hostname_owner.id != (worker.id if worker else None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the worker hostname is already bound to another device identity",
        )
    secret = settings.require_session_secret()
    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_session_token(raw_token, secret)
    credential_expires_at = now + timedelta(days=settings.worker_credential_days)
    is_new = worker is None
    previous_hostname = worker.hostname_label if worker is not None else None
    if worker is None:
        worker = Worker(
            token_hash=token_hash,
            certificate_subject=device_subject,
            last_enrollment_nonce_hash=nonce_hash,
            hostname_label=payload.heartbeat.hostname_label,
            architecture=payload.heartbeat.architecture,
            operating_system=payload.heartbeat.operating_system,
            software_version=payload.heartbeat.software_version,
            capabilities=payload.heartbeat.capabilities,
            runtime_status=_runtime_status(payload.heartbeat),
            allowed_capabilities=sorted(requested_capabilities),
            state="active",
            credential_rotated_at=now,
        )
        session.add(worker)
        await session.flush()
    else:
        await session.execute(
            update(WorkerCredential)
            .where(WorkerCredential.worker_id == worker.id, WorkerCredential.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        worker.token_hash = token_hash
        worker.last_enrollment_nonce_hash = nonce_hash
        worker.hostname_label = payload.heartbeat.hostname_label
        worker.architecture = payload.heartbeat.architecture
        worker.operating_system = payload.heartbeat.operating_system
        worker.software_version = payload.heartbeat.software_version
        worker.capabilities = payload.heartbeat.capabilities
        worker.runtime_status = _runtime_status(payload.heartbeat)
        worker.allowed_capabilities = sorted(requested_capabilities)
        worker.state = "active"
        worker.credential_rotated_at = now
    credential = WorkerCredential(
        worker_id=worker.id,
        token_hash=token_hash,
        allowed_routing_keys=sorted(_routes_for_capabilities(requested_capabilities)),
        allowed_data_classes=sorted(DEFAULT_DATA_CLASSES),
        allowed_connection_ids=[],
        issued_at=now,
        expires_at=credential_expires_at,
    )
    session.add(credential)
    if previous_hostname is not None and previous_hostname != worker.hostname_label:
        await append_audit_event(
            session,
            AuditRecord(
                event_type="worker.renamed",
                actor_type="worker",
                actor_id=str(worker.id),
                target_ref={"worker_id": str(worker.id)},
                correlation_id=correlation_id,
                before={"hostname_label": previous_hostname},
                after={"hostname_label": worker.hostname_label},
            ),
        )
        await append_operation_event(
            session,
            OperationRecord(
                source="worker",
                event_type="worker.renamed",
                message="Local worker adopted its configured name",
                correlation_id=correlation_id,
                worker_id=worker.id,
                details={
                    "previous_hostname_label": previous_hostname,
                    "hostname_label": worker.hostname_label,
                    "authentication": "signed-device-identity",
                },
            ),
        )
    event_type = "worker.auto_enrolled" if is_new else "worker.credential.recovered"
    await append_audit_event(
        session,
        AuditRecord(
            event_type=event_type,
            actor_type="worker",
            actor_id=str(worker.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            after={
                "hostname": worker.hostname_label,
                "capabilities": sorted(requested_capabilities),
                "authentication": "signed-device-identity",
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type=event_type,
            message=(
                "Local worker joined automatically"
                if is_new
                else "Local worker recovered its credential automatically"
            ),
            correlation_id=correlation_id,
            worker_id=worker.id,
            details={
                "hostname_label": worker.hostname_label,
                "source_address": source_address,
                "authentication": "signed-device-identity",
            },
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return WorkerEnrollmentResponse(
        worker_token=raw_token,
        credential_expires_at=credential_expires_at,
        worker=_worker_response(worker, settings),
    )
