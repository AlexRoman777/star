from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import Asset, NormalizedDerivative
from star.services.media_validation import decoded_image_is_blank
from star.services.storage_inventory import (
    evaluate_artifact_write_admission,
    raise_storage_admission_error,
)

NORMALIZED_TRANSFORM_VERSION = "fit-jpeg-q80-v1"
NormalizedVariant = Literal["thumbnail", "preview"]
NORMALIZED_MEDIA_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class CachedNormalizedDerivative:
    content_path: Path
    content_digest: str
    size_bytes: int
    width: int
    height: int


def _relative_content_path(digest: str) -> Path:
    return Path("sha256") / digest[:2] / digest


def _validate_jpeg(
    content: bytes,
    *,
    variant: NormalizedVariant,
) -> tuple[str, int, int]:
    if not 0 < len(content) <= NORMALIZED_MEDIA_LIMIT:
        raise ValueError("normalized JPEG is outside the bounded byte contract")
    digest = hashlib.sha256(content).hexdigest()
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "JPEG":
                raise ValueError("normalized derivative must be JPEG")
            width, height = image.size
            image.verify()
        if decoded_image_is_blank(content):
            raise ValueError("normalized derivative is blank or visually unusable")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("normalized derivative could not be decoded safely") from exc
    edge_limit = 720 if variant == "thumbnail" else 2048
    if width < 16 or height < 16 or max(width, height) > edge_limit:
        raise ValueError("normalized derivative dimensions exceed its variant contract")
    return digest, width, height


def _write_content_addressed(root: Path, relative: Path, content: bytes) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = destination.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        if stat.S_ISREG(current.st_mode) and int(current.st_size) == len(content):
            return
        raise OSError("normalized cache destination is not a matching regular file")
    temporary = destination.with_name(f"{destination.name}.partial-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


async def store_normalized_derivative(
    session: AsyncSession,
    settings: Settings,
    *,
    asset: Asset,
    variant: NormalizedVariant,
    content: bytes,
) -> NormalizedDerivative:
    if asset.current_revision_id is None:
        raise ValueError("asset has no current revision")
    digest, width, height = await asyncio.to_thread(
        _validate_jpeg,
        content,
        variant=variant,
    )
    admission = await evaluate_artifact_write_admission(
        session,
        settings,
        incoming_bytes=len(content),
        content_digest=digest,
    )
    if not admission.allowed:
        await raise_storage_admission_error(session, admission)
    relative = _relative_content_path(digest)
    await asyncio.to_thread(_write_content_addressed, settings.artifact_root, relative, content)
    derivative = await session.scalar(
        select(NormalizedDerivative).where(
            NormalizedDerivative.asset_id == asset.id,
            NormalizedDerivative.asset_revision_id == asset.current_revision_id,
            NormalizedDerivative.variant == variant,
            NormalizedDerivative.transform_version == NORMALIZED_TRANSFORM_VERSION,
        )
    )
    now = utc_now()
    if derivative is None:
        derivative = NormalizedDerivative(
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            variant=variant,
            transform_version=NORMALIZED_TRANSFORM_VERSION,
            content_digest=digest,
            relative_path=relative.as_posix(),
            media_type="image/jpeg",
            size_bytes=len(content),
            width=width,
            height=height,
            encoding_quality=80,
            last_accessed_at=now,
        )
        session.add(derivative)
    else:
        derivative.content_digest = digest
        derivative.relative_path = relative.as_posix()
        derivative.size_bytes = len(content)
        derivative.width = width
        derivative.height = height
        derivative.encoding_quality = 80
        derivative.last_accessed_at = now
    await session.flush()
    return derivative


ACCESS_TOUCH_INTERVAL_SECONDS = 3600


async def cached_normalized_derivative(
    session: AsyncSession,
    settings: Settings,
    *,
    asset: Asset,
    variant: NormalizedVariant,
) -> CachedNormalizedDerivative | None:
    if asset.current_revision_id is None:
        return None
    derivative = await session.scalar(
        select(NormalizedDerivative).where(
            NormalizedDerivative.asset_id == asset.id,
            NormalizedDerivative.asset_revision_id == asset.current_revision_id,
            NormalizedDerivative.variant == variant,
            NormalizedDerivative.transform_version == NORMALIZED_TRANSFORM_VERSION,
        )
    )
    if derivative is None:
        return None
    expected = _relative_content_path(derivative.content_digest)
    if derivative.relative_path != expected.as_posix():
        return None
    path = settings.artifact_root / expected
    try:
        current = await asyncio.to_thread(path.lstat)
    except FileNotFoundError, OSError:
        return None
    if not stat.S_ISREG(current.st_mode) or int(current.st_size) != derivative.size_bytes:
        return None
    # Retention only needs coarse recency. Touching the row on every read turned
    # each gallery thumbnail into a database write; once an hour is plenty.
    now = utc_now()
    accessed = derivative.last_accessed_at
    if accessed is not None and accessed.tzinfo is None:
        accessed = accessed.replace(tzinfo=now.tzinfo)
    if accessed is None or (now - accessed).total_seconds() >= ACCESS_TOUCH_INTERVAL_SECONDS:
        derivative.last_accessed_at = now
    return CachedNormalizedDerivative(
        content_path=path,
        content_digest=derivative.content_digest,
        size_bytes=derivative.size_bytes,
        width=derivative.width,
        height=derivative.height,
    )
