from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal
from star.api.routers.assets._shared import (
    viewer,
)
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.models import (
    Asset,
    ImmichConnection,
    ImmichInstance,
    PersonReference,
)
from star.db.session import get_session
from star.domain.enums import (
    CredentialKind,
)
from star.immich.client import ImmichApiError, ImmichClient
from star.services.connections import get_active_credential
from star.services.media_relay import (
    MediaRelayError,
    media_relay_http_status,
    open_asset_media_response,
)
from star.services.normalized_derivatives import cached_normalized_derivative
from star.services.visual_revision import current_visual_revision

router = APIRouter(tags=["Assets"])


@dataclass(frozen=True, slots=True)
class _HotDerivative:
    """A recently served derivative: enough to answer again without the database."""

    content_path: Path
    content_digest: str
    size_bytes: int
    visual_version: str
    expires_at: float


# Derivatives are content-addressed and bound to one asset revision, so a short
# in-process memo is safe: a changed revision changes the visual version, which
# the gallery sends as ``?v=`` and which is checked before serving. Eviction on
# the NAS is caught by the size check and falls back to the database path.
HOT_DERIVATIVE_TTL_SECONDS = 600
HOT_DERIVATIVE_LIMIT = 8192
_hot_derivatives: OrderedDict[tuple[uuid.UUID, str], _HotDerivative] = OrderedDict()


def _remember_hot(key: tuple[uuid.UUID, str], entry: _HotDerivative) -> None:
    _hot_derivatives[key] = entry
    _hot_derivatives.move_to_end(key)
    while len(_hot_derivatives) > HOT_DERIVATIVE_LIMIT:
        _hot_derivatives.popitem(last=False)


def _recall_hot(key: tuple[uuid.UUID, str]) -> _HotDerivative | None:
    entry = _hot_derivatives.get(key)
    if entry is None:
        return None
    if entry.expires_at < time.monotonic():
        _hot_derivatives.pop(key, None)
        return None
    _hot_derivatives.move_to_end(key)
    return entry


def _derivative_response(
    content: bytes, digest: str, expected_visual_version: str | None
) -> Response:
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if expected_visual_version is not None
                else "private, max-age=86400, stale-while-revalidate=604800"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Star-Cache": "normalized-jpeg-q80",
            "ETag": f'"{digest}"',
        },
    )


async def _serve_hot_derivative(
    asset_id: uuid.UUID,
    variant: Literal["thumbnail", "preview"],
    expected_visual_version: str | None,
    if_none_match: str | None,
) -> Response | None:
    entry = _recall_hot((asset_id, variant))
    if entry is None:
        return None
    if expected_visual_version is not None and expected_visual_version != entry.visual_version:
        return None
    etag = f'"{entry.content_digest}"'
    if if_none_match and etag in if_none_match:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    try:
        content = await asyncio.to_thread(entry.content_path.read_bytes)
    except OSError:
        _hot_derivatives.pop((asset_id, variant), None)
        return None
    if len(content) != entry.size_bytes:
        _hot_derivatives.pop((asset_id, variant), None)
        return None
    return _derivative_response(content, entry.content_digest, expected_visual_version)


async def _cached_asset_media(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    variant: Literal["thumbnail", "preview"],
    expected_visual_version: str | None,
) -> Response | None:
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.access_state != "available":
        return None
    current = await current_visual_revision(session, asset)
    if current is None or (
        expected_visual_version is not None and expected_visual_version != current[1]
    ):
        return None
    cached = await cached_normalized_derivative(
        session,
        settings,
        asset=asset,
        variant=variant,
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
    _remember_hot(
        (asset_id, variant),
        _HotDerivative(
            content_path=cached.content_path,
            content_digest=cached.content_digest,
            size_bytes=cached.size_bytes,
            visual_version=current[1],
            expires_at=time.monotonic() + HOT_DERIVATIVE_TTL_SECONDS,
        ),
    )
    return _derivative_response(content, cached.content_digest, expected_visual_version)


@router.get("/person-thumbnails/{person_reference_id}")
async def get_person_thumbnail(
    person_reference_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> Response:
    person = await session.get(PersonReference, person_reference_id)
    if person is None or person.access_state != "available":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
    connection = await session.get(ImmichConnection, person.connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="person connection is unavailable",
        )
    instance = await session.get(ImmichInstance, connection.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=person.connection_id,
        kind=CredentialKind.READ,
    )
    if instance is None or credential is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="person connection is unavailable",
        )
    api_key = SecretCipher(settings.require_encryption_key()).decrypt(credential.encrypted_secret)
    person_id = person.immich_person_id
    base_url = instance.base_url
    await session.commit()
    try:
        async with ImmichClient(base_url, api_key) as client:
            preview = await client.get_person_thumbnail(person_id)
    except ImmichApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich person thumbnail request failed",
        ) from exc
    if len(preview.content) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich person thumbnail exceeded the safety limit",
        )
    return Response(
        content=preview.content,
        media_type=preview.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{asset_id}/media/preview")
async def get_asset_preview(
    asset_id: uuid.UUID,
    request: Request,
    variant: Literal["preview", "gallery"] = Query(default="preview"),
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> Response:
    requested_version = request.query_params.get("v")
    cached_variant: Literal["thumbnail", "preview"] = (
        "thumbnail" if variant == "gallery" else "preview"
    )
    hot = await _serve_hot_derivative(
        asset_id, cached_variant, requested_version, request.headers.get("if-none-match")
    )
    if hot is not None:
        return hot
    cached = await _cached_asset_media(
        session,
        settings,
        asset_id,
        variant=cached_variant,
        expected_visual_version=requested_version,
    )
    if cached is not None:
        return cached
    try:
        return await open_asset_media_response(
            session,
            settings,
            asset_id,
            size="thumbnail" if variant == "gallery" else "preview",
            expected_visual_version=requested_version,
            cache_control=(
                "private, max-age=31536000, immutable"
                if requested_version is not None
                else "private, max-age=86400, stale-while-revalidate=604800"
            ),
        )
    except MediaRelayError as exc:
        raise HTTPException(
            status_code=media_relay_http_status(exc),
            detail="Immich preview unavailable",
            headers={"Cache-Control": "no-store"},
        ) from exc


@router.get("/{asset_id}/media/thumbnail")
async def get_asset_thumbnail(
    asset_id: uuid.UUID,
    request: Request,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> Response:
    requested_version = request.query_params.get("v")
    hot = await _serve_hot_derivative(
        asset_id, "thumbnail", requested_version, request.headers.get("if-none-match")
    )
    if hot is not None:
        return hot
    cached = await _cached_asset_media(
        session,
        settings,
        asset_id,
        variant="thumbnail",
        expected_visual_version=requested_version,
    )
    if cached is not None:
        return cached
    try:
        return await open_asset_media_response(
            session,
            settings,
            asset_id,
            size="thumbnail",
            expected_visual_version=requested_version,
            cache_control=(
                "private, max-age=31536000, immutable"
                if requested_version is not None
                else "private, max-age=86400, stale-while-revalidate=604800"
            ),
        )
    except MediaRelayError as exc:
        raise HTTPException(
            status_code=media_relay_http_status(exc),
            detail="Immich thumbnail unavailable",
            headers={"Cache-Control": "no-store"},
        ) from exc
