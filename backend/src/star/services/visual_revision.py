from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import Asset, AssetRevision

_VISUAL_METADATA_FIELDS = (
    "checksum",
    "thumbhash",
    "width",
    "height",
    "type",
    "isEdited",
    "originalMimeType",
    "livePhotoVideoId",
)


def visual_revision_key(revision: AssetRevision) -> str:
    visual = {field: revision.normalized_metadata.get(field) for field in _VISUAL_METADATA_FIELDS}
    visual["width"] = revision.width if revision.width is not None else visual["width"]
    visual["height"] = revision.height if revision.height is not None else visual["height"]
    encoded = repr(sorted(visual.items())).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def current_visual_revision(
    session: AsyncSession,
    asset: Asset,
) -> tuple[AssetRevision, str] | None:
    if asset.current_revision_id is None:
        return None
    revision = await session.get(AssetRevision, asset.current_revision_id)
    if revision is None:
        return None
    return revision, visual_revision_key(revision)
