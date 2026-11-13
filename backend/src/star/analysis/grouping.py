from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import (
    Asset,
    AssetRevision,
    CleanupCampaign,
    SimilarityGroup,
    SimilarityGroupMember,
)
from star.domain.enums import GroupType

RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".nef",
    ".orf",
    ".raf",
    ".raw",
    ".rw2",
}
IMAGE_EXTENSIONS = RAW_EXTENSIONS | {
    ".avif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


@dataclass(frozen=True)
class AssetObservation:
    asset: Asset
    revision: AssetRevision
    filename: str
    extension: str
    capture_time: datetime | None
    checksum: str | None
    camera_key: str

    @property
    def is_raw(self) -> bool:
        return self.extension in RAW_EXTENSIONS

    @property
    def pixels(self) -> int:
        return (self.revision.width or 0) * (self.revision.height or 0)


def _string(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _capture_time(metadata: dict[str, Any]) -> datetime | None:
    value = _string(metadata, "localDateTime", "fileCreatedAt", "dateTimeOriginal")
    if not value:
        exif = metadata.get("exifInfo")
        if isinstance(exif, dict):
            value = _string(exif, "dateTimeOriginal", "modifyDate")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _camera_key(metadata: dict[str, Any]) -> str:
    exif = metadata.get("exifInfo")
    if not isinstance(exif, dict):
        return "unknown"
    return (_string(exif, "make") + ":" + _string(exif, "model")).lower()


def _filename(metadata: dict[str, Any]) -> str:
    return _string(metadata, "originalFileName", "fileName", "originalPath")


def _checksum(metadata: dict[str, Any]) -> str | None:
    value = _string(metadata, "checksum", "sha1", "fileChecksum")
    return value or None


def _group_key(group_type: GroupType, identity: str) -> str:
    return hashlib.sha256((group_type.value + ":" + identity).encode()).hexdigest()


def _protection_reasons(observation: AssetObservation) -> list[str]:
    reasons: list[str] = []
    if observation.is_raw:
        reasons.append("raw_original")
    if observation.revision.rating is not None:
        reasons.append("existing_rating")
    if observation.revision.is_favorite:
        reasons.append("favorite")
    if observation.capture_time and observation.capture_time.year <= 2000:
        reasons.append("older_family_media")
    metadata = observation.revision.normalized_metadata
    if metadata.get("stackParentId") or metadata.get("isOffline"):
        reasons.append("immich_relationship")
    return reasons


async def _observations(
    session: AsyncSession,
    campaign: CleanupCampaign,
) -> list[AssetObservation]:
    connection_values = campaign.scope.get("connection_ids", [])
    connection_ids: list[uuid.UUID] = []
    for value in connection_values:
        try:
            connection_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not connection_ids:
        return []
    statement = (
        select(Asset, AssetRevision)
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .where(
            Asset.connection_id.in_(connection_ids),
            Asset.access_state == "available",
        )
    )
    asset_values = campaign.scope.get("asset_ids", [])
    asset_ids: list[uuid.UUID] = []
    for value in asset_values:
        try:
            asset_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if asset_values:
        if not asset_ids:
            return []
        statement = statement.where(Asset.id.in_(asset_ids))
    rows = (await session.execute(statement)).all()
    date_from = campaign.scope.get("date_from")
    date_to = campaign.scope.get("date_to")
    result: list[AssetObservation] = []
    for asset, revision in rows:
        metadata = revision.normalized_metadata
        capture = _capture_time(metadata)
        capture_date = capture.date().isoformat() if capture else None
        if (date_from or date_to) and capture_date is None:
            continue
        if date_from and capture_date and capture_date < str(date_from):
            continue
        if date_to and capture_date and capture_date > str(date_to):
            continue
        filename = _filename(metadata)
        extension = PurePath(filename).suffix.lower()
        result.append(
            AssetObservation(
                asset=asset,
                revision=revision,
                filename=filename,
                extension=extension,
                capture_time=capture,
                checksum=_checksum(metadata),
                camera_key=_camera_key(metadata),
            )
        )
    return result


async def _store_group(
    session: AsyncSession,
    *,
    campaign: CleanupCampaign,
    group_type: GroupType,
    identity: str,
    observations: list[AssetObservation],
    confidence: float,
    explanation: str,
    estimate_cleanup: bool,
) -> bool:
    if len(observations) < 2:
        return False
    key = _group_key(group_type, identity)
    if await session.scalar(
        select(SimilarityGroup.id).where(
            SimilarityGroup.campaign_id == campaign.id,
            SimilarityGroup.group_key == key,
        )
    ):
        return False
    ranked = sorted(
        observations,
        key=lambda item: (item.pixels, item.revision.file_size or 0),
        reverse=True,
    )
    estimated = sum(item.revision.file_size or 0 for item in ranked[1:]) if estimate_cleanup else 0
    group = SimilarityGroup(
        library_program_id=campaign.library_program_id,
        campaign_id=campaign.id,
        group_key=key,
        group_type=group_type,
        confidence=confidence,
        estimated_recoverable_bytes=estimated,
        explanation=explanation,
    )
    session.add(group)
    await session.flush()
    for index, observation in enumerate(ranked):
        reasons = _protection_reasons(observation)
        session.add(
            SimilarityGroupMember(
                group_id=group.id,
                asset_id=observation.asset.id,
                role="preserve_candidate" if index == 0 else "comparison_candidate",
                rank=index + 1,
                confidence=confidence,
                is_protected=bool(reasons),
                protection_reasons=reasons,
            )
        )
    return True


async def discover_metadata_groups(
    session: AsyncSession,
    campaign: CleanupCampaign,
) -> dict[str, int]:
    """Create conservative, explainable groups without downloading media bytes."""
    observations = await _observations(session, campaign)
    created: defaultdict[str, int] = defaultdict(int)

    exact: defaultdict[str, list[AssetObservation]] = defaultdict(list)
    for observation in observations:
        if observation.checksum:
            exact[observation.checksum].append(observation)
    for checksum, members in exact.items():
        if await _store_group(
            session,
            campaign=campaign,
            group_type=GroupType.EXACT_BINARY,
            identity=checksum,
            observations=members,
            confidence=1.0,
            explanation=(
                "Immich metadata reports the same file checksum. Human review still "
                "controls any cleanup action."
            ),
            estimate_cleanup=True,
        ):
            created[GroupType.EXACT_BINARY.value] += 1

    pairs: defaultdict[str, list[AssetObservation]] = defaultdict(list)
    for observation in observations:
        if observation.extension not in IMAGE_EXTENSIONS or not observation.filename:
            continue
        stem = PurePath(observation.filename).stem.lower()
        time_bucket = (
            observation.capture_time.strftime("%Y%m%dT%H%M")
            if observation.capture_time
            else "unknown"
        )
        pairs[stem + ":" + time_bucket].append(observation)
    for identity, members in pairs.items():
        if not any(item.is_raw for item in members) or not any(not item.is_raw for item in members):
            continue
        if await _store_group(
            session,
            campaign=campaign,
            group_type=GroupType.RAW_JPEG,
            identity=identity,
            observations=members,
            confidence=0.95 if ":unknown" not in identity else 0.7,
            explanation=(
                "Matching capture name and time suggest a RAW plus rendered-image "
                "relationship. RAW members are protected."
            ),
            estimate_cleanup=False,
        ):
            created[GroupType.RAW_JPEG.value] += 1

    timed = sorted(
        (item for item in observations if item.capture_time and item.extension in IMAGE_EXTENSIONS),
        key=lambda item: (
            str(item.asset.connection_id),
            item.camera_key,
            item.capture_time or datetime.min.replace(tzinfo=UTC),
        ),
    )
    run: list[AssetObservation] = []
    for item in timed:
        if not run:
            run = [item]
            continue
        previous = run[-1]
        same_source = (
            item.asset.connection_id == previous.asset.connection_id
            and item.camera_key == previous.camera_key
        )
        close = item.capture_time - previous.capture_time <= timedelta(seconds=2)  # type: ignore[operator]
        if same_source and close:
            run.append(item)
            continue
        if len(run) >= 2:
            identity = str(run[0].asset.connection_id) + ":" + run[0].capture_time.isoformat()  # type: ignore[union-attr]
            if await _store_group(
                session,
                campaign=campaign,
                group_type=GroupType.BURST,
                identity=identity,
                observations=run,
                confidence=0.72,
                explanation=(
                    "Consecutive captures from the same account and camera are grouped "
                    "as a possible burst, not as duplicates."
                ),
                estimate_cleanup=False,
            ):
                created[GroupType.BURST.value] += 1
        run = [item]
    if len(run) >= 2:
        identity = str(run[0].asset.connection_id) + ":" + run[0].capture_time.isoformat()  # type: ignore[union-attr]
        if await _store_group(
            session,
            campaign=campaign,
            group_type=GroupType.BURST,
            identity=identity,
            observations=run,
            confidence=0.72,
            explanation=(
                "Consecutive captures from the same account and camera are grouped as "
                "a possible burst, not as duplicates."
            ),
            estimate_cleanup=False,
        ):
            created[GroupType.BURST.value] += 1
    await session.flush()
    return {"assets_considered": len(observations), **dict(created)}
