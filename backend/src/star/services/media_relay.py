from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageFile, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from starlette.responses import StreamingResponse

from star.core.config import Settings
from star.core.crypto import SecretCipher
from star.db.models import Asset, ImmichConnection, ImmichInstance
from star.domain.enums import CredentialKind
from star.immich.client import ImmichApiError, ImmichClient
from star.services.connections import get_active_credential
from star.services.media_metrics import (
    IMMICH_FULLSIZE_REQUESTS,
    IMMICH_PREVIEW_BYTES,
    IMMICH_PREVIEW_FAILURES,
    IMMICH_PREVIEW_REQUESTS,
    IMMICH_REDIRECTS_REJECTED,
    IMMICH_THUMBNAIL_REQUESTS,
    WORKER_MEDIA_STREAM_FAILURES,
)
from star.services.media_validation import decoded_image_is_blank, image_is_blank
from star.services.visual_revision import current_visual_revision

logger = logging.getLogger(__name__)
DerivativeSize = Literal["thumbnail", "preview", "fullsize"]
MEDIA_CONTRACT_VERSION = "immich-720p-thumbnail__4k-preview__full-dimension-webp-v1"
DERIVATIVE_ROLES = {
    "thumbnail": "720p_thumbnail",
    "preview": "4k_preview",
    "fullsize": "full_dimension_webp",
}
# What the model is finally shown is always a re-encoded JPEG, so this set is
# not a model contract: it is only the shortlist Immich is expected to declare.
ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
# A library shot on several cameras makes Immich serve previews it labels in
# other ways, and refusing those on the label alone rejected photographs that
# open perfectly well. Anything the decoder can actually read is acceptable;
# the byte and pixel ceilings still apply, and a non-image declaration is still
# refused before a single byte reaches the decoder.
DECODABLE_MEDIA_TYPES = frozenset(
    {
        *ALLOWED_MEDIA_TYPES,
        "image/avif",
        "image/tiff",
        "image/bmp",
        "image/gif",
        "image/jp2",
        "image/jpx",
        "image/x-portable-pixmap",
    }
)


def media_type_is_decodable(media_type: str) -> bool:
    """Return whether the relay should even try to decode this declaration.

    An empty or generic declaration is worth attempting, because some servers
    send ``application/octet-stream`` for a perfectly ordinary JPEG. Anything
    that positively claims to be something other than an image is not.
    """

    if not media_type or media_type == "application/octet-stream":
        return True
    if media_type in DECODABLE_MEDIA_TYPES:
        return True
    return media_type.startswith("image/")


HEADER_PROBE_LIMIT = 1024 * 1024
_STREAM_LIMITS: dict[tuple[int, int], asyncio.Semaphore] = {}


class MediaRelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetMediaSource:
    base_url: str
    api_key: str
    immich_asset_id: str
    visual_version: str


@dataclass(frozen=True)
class BufferedDerivative:
    content: bytes
    media_type: str
    width: int
    height: int
    digest: str


async def _buffer_valid_derivative(
    response: httpx.Response,
    *,
    max_bytes: int,
    max_pixels: int,
) -> BufferedDerivative:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not media_type_is_decodable(media_type):
        raise MediaRelayError("unsupported_media_type")
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise MediaRelayError("oversized")
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise MediaRelayError("pixel_limit")
            image.load()
            if image_is_blank(image):
                raise MediaRelayError("blank_image")
    except (OSError, UnidentifiedImageError) as exc:
        # Distinguish "we were never going to read this" from "this file is
        # broken", so the owner is told which one it is instead of a guess.
        if media_type not in DECODABLE_MEDIA_TYPES:
            raise MediaRelayError("unsupported_media_type") from exc
        raise MediaRelayError("invalid_image") from exc
    return BufferedDerivative(
        content=content,
        media_type=media_type,
        width=width,
        height=height,
        digest=hashlib.sha256(content).hexdigest(),
    )


async def _buffered_chunks(content: bytes, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), chunk_size):
        yield content[offset : offset + chunk_size]


class PreviewRelay:
    def __init__(
        self,
        *,
        client: ImmichClient,
        response: httpx.Response,
        media_type: str,
        max_bytes: int,
        max_pixels: int,
        worker_stream: bool,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self.client = client
        self.response = response
        self.media_type = media_type
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.worker_stream = worker_stream
        self.semaphore = semaphore
        self.closed = False
        self._iterator = response.aiter_bytes().__aiter__()
        self._buffered: list[bytes] = []
        self._received = 0
        self._prepared = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.response.aclose()
        await self.client.close()
        self.semaphore.release()

    def _record_failure(self, error_class: str) -> None:
        IMMICH_PREVIEW_FAILURES.labels(error_class=error_class).inc()
        if self.worker_stream:
            WORKER_MEDIA_STREAM_FAILURES.labels(error_class=error_class).inc()

    async def prepare(self) -> None:
        """Fully validate the bounded image before the HTTP response starts.

        Previews and thumbnails are already bounded by ``max_bytes``, so the
        whole body is buffered and decoded here. A truncated body or a blank
        (uniform) derivative must fail before any byte reaches a worker or the
        durable normalized cache, matching the fullsize path's validation.
        """

        if self._prepared:
            return
        parser = ImageFile.Parser()
        try:
            while parser.image is None:
                try:
                    chunk = await anext(self._iterator)
                except StopAsyncIteration as exc:
                    raise MediaRelayError("immich_preview_invalid_image") from exc
                if not chunk:
                    continue
                self._received += len(chunk)
                if self._received > self.max_bytes:
                    raise MediaRelayError("immich_preview_oversized")
                self._buffered.append(chunk)
                parser.feed(chunk)
                if parser.image is None and self._received > HEADER_PROBE_LIMIT:
                    raise MediaRelayError("immich_preview_invalid_image")
            width, height = parser.image.size
            if width <= 0 or height <= 0 or width * height > self.max_pixels:
                raise MediaRelayError("immich_preview_pixel_limit")
            async for chunk in self._iterator:
                if not chunk:
                    continue
                self._received += len(chunk)
                if self._received > self.max_bytes:
                    raise MediaRelayError("immich_preview_oversized")
                self._buffered.append(chunk)
            content = b"".join(self._buffered)
            self._buffered = [content]
            try:
                blank = await asyncio.to_thread(decoded_image_is_blank, content)
            except (OSError, UnidentifiedImageError) as exc:
                raise MediaRelayError("immich_preview_invalid_image") from exc
            if blank:
                raise MediaRelayError("immich_preview_blank_image")
            self._prepared = True
        except asyncio.CancelledError:
            await self.close()
            raise
        except MediaRelayError as exc:
            self._record_failure(str(exc))
            await self.close()
            raise

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            await self.prepare()
            for chunk in self._buffered:
                IMMICH_PREVIEW_BYTES.inc(len(chunk))
                yield chunk
            self._buffered.clear()
            async for chunk in self._iterator:
                if not chunk:
                    continue
                self._received += len(chunk)
                if self._received > self.max_bytes:
                    raise MediaRelayError("immich_preview_oversized")
                IMMICH_PREVIEW_BYTES.inc(len(chunk))
                yield chunk
        except asyncio.CancelledError:
            raise
        except MediaRelayError as exc:
            self._record_failure(str(exc))
            raise
        finally:
            await self.close()


async def resolve_asset_media_source(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    expected_visual_version: str | None = None,
) -> AssetMediaSource:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise MediaRelayError("asset_not_found")
    current = await current_visual_revision(session, asset)
    if asset.access_state != "available" or current is None or current[0].is_trashed:
        raise MediaRelayError("asset_unavailable")
    visual_version = current[1]
    if expected_visual_version is not None and expected_visual_version != visual_version:
        raise MediaRelayError("preview_version_stale")
    connection = await session.get(ImmichConnection, asset.connection_id)
    instance = await session.get(ImmichInstance, asset.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=asset.connection_id,
        kind=CredentialKind.READ,
    )
    if connection is None or instance is None or credential is None:
        raise MediaRelayError("asset_connection_unavailable")
    api_key = SecretCipher(settings.require_encryption_key()).decrypt(credential.encrypted_secret)
    return AssetMediaSource(
        base_url=settings.immich_internal_url or instance.base_url,
        api_key=api_key,
        immich_asset_id=asset.immich_asset_id,
        visual_version=visual_version,
    )


async def _validate_private_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaRelayError("immich_internal_url_invalid")
    try:
        addresses = {ipaddress.ip_address(parsed.hostname)}
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            resolved = await loop.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise MediaRelayError("immich_internal_url_unresolvable") from exc
        addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
    if not addresses or any(
        not (address.is_private or address.is_loopback or address.is_link_local)
        for address in addresses
    ):
        raise MediaRelayError("immich_internal_url_not_private")


class _PooledImmichTransport(httpx.AsyncHTTPTransport):
    """Connection pool that survives the relay's per-request client close.

    Media relay deliberately constructs one short-lived client per request;
    sharing the transport keeps warm TCP connections toward Immich without
    changing that per-request lifecycle.
    """

    async def aclose(self) -> None:
        return None


_immich_transports: dict[int, _PooledImmichTransport] = {}


def _shared_immich_transport() -> httpx.AsyncBaseTransport:
    key = id(asyncio.get_running_loop())
    transport = _immich_transports.get(key)
    if transport is None:
        transport = _PooledImmichTransport(
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60.0)
        )
        _immich_transports[key] = transport
    return transport


def _stream_semaphore(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = (id(loop), limit)
    semaphore = _STREAM_LIMITS.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _STREAM_LIMITS[key] = semaphore
    return semaphore


async def open_asset_media_response(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    size: DerivativeSize,
    expected_visual_version: str | None = None,
    worker_stream: bool = False,
    cache_control: str = "no-store",
    background: bool = False,
) -> StreamingResponse:
    from star.services.media_permits import (
        MediaPermitUnavailable,
        guarded_media_call,
        permit_factory,
    )

    bind = session.bind
    if bind is None:
        raise MediaRelayError("immich_preview_busy")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    await session.commit()
    try:
        return await guarded_media_call(
            permit_factory(engine),
            lambda: _open_asset_media_response(
                session,
                settings,
                asset_id,
                size=size,
                expected_visual_version=expected_visual_version,
                worker_stream=worker_stream,
                cache_control=cache_control,
            ),
            background=background,
        )
    except MediaPermitUnavailable as exc:
        raise MediaRelayError("immich_preview_busy") from exc


async def _open_asset_media_response(
    session: AsyncSession,
    settings: Settings,
    asset_id: uuid.UUID,
    *,
    size: DerivativeSize,
    expected_visual_version: str | None = None,
    worker_stream: bool = False,
    cache_control: str = "no-store",
) -> StreamingResponse:
    source = await resolve_asset_media_source(
        session,
        settings,
        asset_id,
        expected_visual_version=expected_visual_version,
    )
    await _validate_private_base_url(source.base_url)
    await session.commit()
    from star.services.resource_pressure import (
        current_pressure_decision,
        observe_immich_latency,
    )

    semaphore = _stream_semaphore(current_pressure_decision().preview_concurrency)
    await semaphore.acquire()
    client = ImmichClient(source.base_url, source.api_key, transport=_shared_immich_transport())
    started_at = asyncio.get_running_loop().time()
    if size == "fullsize":
        fallback_reason: str | None = None
        last_error = "immich_media_unavailable"
        try:
            for refresh_attempt in range(2):
                for variant in ("fullsize", "preview"):
                    if variant == "fullsize":
                        IMMICH_FULLSIZE_REQUESTS.inc()
                    else:
                        IMMICH_PREVIEW_REQUESTS.inc()
                    try:
                        response = await client.open_thumbnail_stream(
                            source.immich_asset_id,
                            size=variant,
                            max_bytes=settings.preview_stream_max_bytes,
                        )
                    except ImmichApiError as exc:
                        if exc.status_code is not None and 300 <= exc.status_code < 400:
                            IMMICH_REDIRECTS_REJECTED.inc()
                            reason = "redirect_rejected"
                        else:
                            reason = "upstream_failed"
                        if variant == "fullsize":
                            fallback_reason = reason
                        last_error = reason
                        continue
                    try:
                        derivative = await _buffer_valid_derivative(
                            response,
                            max_bytes=settings.preview_stream_max_bytes,
                            max_pixels=settings.artifact_max_image_pixels,
                        )
                    except MediaRelayError as exc:
                        reason = str(exc)
                        if variant == "fullsize":
                            fallback_reason = reason
                        last_error = reason
                        await response.aclose()
                        continue
                    await response.aclose()
                    observe_immich_latency(
                        (asyncio.get_running_loop().time() - started_at) * 1000.0
                    )
                    if variant == "preview":
                        IMMICH_PREVIEW_BYTES.inc(len(derivative.content))
                    await client.close()
                    semaphore.release()
                    headers = {
                        "Cache-Control": cache_control,
                        "X-Content-Type-Options": "nosniff",
                        "X-Star-Input-Variant": variant,
                        "X-Star-Media-Contract": MEDIA_CONTRACT_VERSION,
                        "X-Star-Source-Role": DERIVATIVE_ROLES[variant],
                        "X-Star-Preview-Version": source.visual_version,
                        "X-Star-Source-Dimensions": (f"{derivative.width}x{derivative.height}"),
                        "X-Star-Source-Digest": derivative.digest,
                    }
                    if fallback_reason is not None:
                        headers["X-Star-Fallback-Reason"] = fallback_reason
                    return StreamingResponse(
                        _buffered_chunks(derivative.content),
                        media_type=derivative.media_type,
                        headers=headers,
                    )
                if refresh_attempt == 0:
                    with suppress(ImmichApiError):
                        await client.get_asset(source.immich_asset_id)
        except asyncio.CancelledError:
            await client.close()
            semaphore.release()
            raise
        except Exception as exc:
            logger.exception(
                "media relay fullsize path failed",
                extra={
                    "asset_id": str(asset_id),
                    "size": size,
                    "visual_version_expected": expected_visual_version,
                    "worker_stream": worker_stream,
                    "error_class": type(exc).__name__,
                },
            )
            await client.close()
            semaphore.release()
            raise
        await client.close()
        semaphore.release()
        IMMICH_PREVIEW_FAILURES.labels(error_class=last_error).inc()
        if worker_stream:
            WORKER_MEDIA_STREAM_FAILURES.labels(error_class=last_error).inc()
        raise MediaRelayError(last_error)
    try:
        response = await client.open_thumbnail_stream(
            source.immich_asset_id,
            size=size,
            max_bytes=settings.preview_stream_max_bytes,
        )
        observe_immich_latency((asyncio.get_running_loop().time() - started_at) * 1000.0)
    except asyncio.CancelledError:
        await client.close()
        semaphore.release()
        raise
    except ImmichApiError as exc:
        observe_immich_latency((asyncio.get_running_loop().time() - started_at) * 1000.0)
        await client.close()
        semaphore.release()
        if exc.status_code is not None and 300 <= exc.status_code < 400:
            IMMICH_REDIRECTS_REJECTED.inc()
        raise MediaRelayError("immich_preview_upstream_failed") from exc
    except Exception as exc:
        logger.exception(
            "media relay preview path failed",
            extra={
                "asset_id": str(asset_id),
                "size": size,
                "visual_version_expected": expected_visual_version,
                "worker_stream": worker_stream,
                "error_class": type(exc).__name__,
            },
        )
        await client.close()
        semaphore.release()
        raise
    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not media_type_is_decodable(media_type):
        await response.aclose()
        await client.close()
        semaphore.release()
        IMMICH_PREVIEW_FAILURES.labels(error_class="unsupported_media_type").inc()
        raise MediaRelayError("immich_preview_unsupported_media_type")
    if size == "thumbnail":
        IMMICH_THUMBNAIL_REQUESTS.inc()
    else:
        IMMICH_PREVIEW_REQUESTS.inc()
    relay = PreviewRelay(
        client=client,
        response=response,
        media_type=media_type,
        max_bytes=settings.preview_stream_max_bytes,
        max_pixels=settings.artifact_max_image_pixels,
        worker_stream=worker_stream,
        semaphore=semaphore,
    )
    await relay.prepare()
    return StreamingResponse(
        relay.chunks(),
        media_type=media_type,
        headers={
            "Cache-Control": cache_control,
            "X-Content-Type-Options": "nosniff",
            "X-Star-Input-Variant": size,
            "X-Star-Media-Contract": MEDIA_CONTRACT_VERSION,
            "X-Star-Source-Role": DERIVATIVE_ROLES[size],
            "X-Star-Preview-Version": source.visual_version,
        },
    )


def media_relay_http_status(error: MediaRelayError) -> int:
    if str(error) in {"asset_not_found", "asset_unavailable"}:
        return 404
    if str(error) == "preview_version_stale":
        return 409
    if str(error) in {"asset_connection_unavailable", "immich_preview_busy"}:
        return 503
    return 502
