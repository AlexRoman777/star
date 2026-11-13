from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.api.routers.analysis_audit import _generation_payload
from star.core.config import Settings, get_settings
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    AuditAccessToken,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.services.auth import resolve_session
from star.services.inference_identity import build_generation_inference_profiles

router = APIRouter(prefix="/audit", tags=["Media-free audit"])
owner = require_roles(Role.ADMINISTRATOR, Role.CURATOR)
AUDIT_SCOPE = "reviews:read"
ALLOWED_AUDIT_SCOPES = frozenset({AUDIT_SCOPE})


@dataclass(frozen=True)
class AuditReader:
    owner_id: uuid.UUID
    token_id: uuid.UUID | None
    scopes: frozenset[str]


class AuditTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=lambda: [AUDIT_SCOPE], min_length=1, max_length=2)


class RepeatabilityRun(BaseModel):
    schema_valid: bool
    rating: int | None = Field(default=None, ge=1, le=5)
    score: float | None = Field(default=None, ge=0, le=100)
    hard_caps: list[str] = Field(default_factory=list, max_length=16)
    privacy_route: str | None = Field(default=None, max_length=64)
    review: dict[str, Any] = Field(default_factory=dict)
    contradiction: bool = False
    winner_id: str | None = Field(default=None, max_length=128)
    series_representative_id: str | None = Field(default=None, max_length=128)


async def _resolve_audit_reader(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    required_scope: str,
) -> AuditReader:
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer ppa_"):
        secret = authorization.removeprefix("Bearer ").strip()
        digest = hash_session_token(secret, settings.require_session_secret())
        token = await session.scalar(
            select(AuditAccessToken).where(AuditAccessToken.token_hash == digest)
        )
        now = datetime.now(UTC)
        if (
            token is None
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
            or required_scope not in token.scopes
        ):
            raise HTTPException(status_code=401, detail="audit token is invalid or expired")
        token.last_used_at = now
        await session.commit()
        return AuditReader(
            owner_id=token.owner_id,
            token_id=token.id,
            scopes=frozenset(token.scopes),
        )
    resolved = await resolve_session(
        session,
        settings,
        request.cookies.get("star_session"),
    )
    if resolved is None:
        raise HTTPException(status_code=401, detail="authentication required")
    user, _user_session = resolved
    allowed_roles = {Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY}
    if user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="permission denied")
    return AuditReader(owner_id=user.id, token_id=None, scopes=frozenset(ALLOWED_AUDIT_SCOPES))


async def require_audit_reader(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuditReader:
    return await _resolve_audit_reader(
        request,
        session,
        settings,
        required_scope=AUDIT_SCOPE,
    )


reader = Annotated[AuditReader, Depends(require_audit_reader)]


@router.post("/tokens")
async def create_audit_token(
    payload: AuditTokenCreate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if payload.expires_at is not None and payload.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expiry must be in the future")
    scopes = list(dict.fromkeys(payload.scopes))
    if any(scope not in ALLOWED_AUDIT_SCOPES for scope in scopes):
        raise HTTPException(status_code=422, detail="unsupported audit scope")
    secret = "ppa_" + secrets.token_urlsafe(32)
    token = AuditAccessToken(
        owner_id=principal.user.id,
        name=payload.name.strip(),
        token_hash=hash_session_token(secret, settings.require_session_secret()),
        scopes=scopes,
        expires_at=payload.expires_at,
    )
    session.add(token)
    await session.commit()
    return {
        "id": str(token.id),
        "name": token.name,
        "token": secret,
        "scopes": token.scopes,
        "expires_at": token.expires_at,
        "shown_once": True,
    }


@router.get("/tokens")
async def list_audit_tokens(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    tokens = list(
        await session.scalars(
            select(AuditAccessToken)
            .where(AuditAccessToken.owner_id == principal.user.id)
            .order_by(AuditAccessToken.created_at.desc())
        )
    )
    return [
        {
            "id": str(token.id),
            "name": token.name,
            "scopes": token.scopes,
            "expires_at": token.expires_at,
            "last_used_at": token.last_used_at,
            "revoked_at": token.revoked_at,
            "created_at": token.created_at,
        }
        for token in tokens
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_audit_token(
    token_id: uuid.UUID,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> None:
    token = await session.get(AuditAccessToken, token_id, with_for_update=True)
    if token is None or token.owner_id != principal.user.id:
        raise HTTPException(status_code=404, detail="audit token not found")
    token.revoked_at = utc_now()
    await session.commit()


async def _current_reviews(
    session: AsyncSession,
    limit: int,
    *,
    after: uuid.UUID | None = None,
    snapshot_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], uuid.UUID | None]:
    snapshot = snapshot_at or utc_now()
    generation_at_snapshot = (
        select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.state == "published",
            AnalysisGeneration.published_at.is_not(None),
            AnalysisGeneration.published_at <= snapshot,
        )
        .order_by(
            AnalysisGeneration.published_at.desc(),
            AnalysisGeneration.sequence.desc(),
            AnalysisGeneration.id.desc(),
        )
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )
    statement = (
        select(Asset, AssetRevision, AnalysisGeneration)
        .join(AnalysisGeneration, AnalysisGeneration.id == generation_at_snapshot)
        .join(AssetRevision, AssetRevision.id == AnalysisGeneration.asset_revision_id)
        .order_by(Asset.id)
        .limit(limit + 1)
    )
    if after is not None:
        statement = statement.where(Asset.id > after)
    rows = list(await session.execute(statement))
    items: list[dict[str, Any]] = []
    for asset, revision, generation in rows[:limit]:
        payload = await _generation_payload(session, generation)
        raw_name = revision.normalized_metadata.get("originalFileName") or ""
        filename = str(raw_name).replace("\\", "/").rsplit("/", 1)[-1][:255]
        items.append({"asset_id": str(asset.id), "filename": filename, "generation": payload})
    next_cursor = rows[limit - 1][0].id if len(rows) > limit and rows else None
    return items, next_cursor


@router.get("/inference-profiles")
async def audit_inference_profiles(
    _reader: reader,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    resolved = await build_generation_inference_profiles(session)
    return [resolved[key] for key in ("primary", "qwen", "comparison")]


@router.get("/resource-pressure")
async def audit_resource_pressure(
    _reader: reader,
    include_history: bool = True,
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    from star.services.resource_pressure import (
        current_pressure_decision,
        persisted_pressure_history,
    )

    history = await persisted_pressure_history(limit=120 if include_history else 1)
    observed = current_pressure_decision().payload()
    if history:
        # The scheduler persists control-state transitions, while this API
        # process can cheaply sample host-visible CPU and memory on every UI
        # refresh. Keep the scheduler's hysteresis decision but replace the
        # resource gauges with a fresh observation.
        current = {
            **history[-1],
            "cpu_percent": observed["cpu_percent"],
            "memory_percent": observed["memory_percent"],
            "swap_percent": observed["swap_percent"],
            "observed_at": observed["sampled_at"],
            "control_sampled_at": history[-1].get("sampled_at"),
        }
    else:
        current = observed
    return {
        **current,
        "measurement_scope": "whole_coordinator_host",
        "star_container_cpu_budget": settings.nas_star_cpu_budget,
        "star_container_memory_budget_bytes": (settings.nas_star_memory_budget_bytes),
        "history": history if include_history else [],
    }
