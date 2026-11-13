from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import (
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetRevision,
    CollectionContextSnapshot,
    CollectionGroup,
    CollectionGroupMember,
)
from star.services.campaigns import canonical_digest

COLLECTION_CONTEXT_PROCESSOR_VERSION = "collection-context-v1"
BURST_DETECTION_VERSION = "burst-metadata-v3-ms-overlap"
INFERRED_BURST_MAX_GAP_MS = 1_500
INFERRED_BURST_MIN_FRAMES = 3
BURST_BACKFILL_OVERLAP = 8


def _text(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _capture_time(metadata: dict[str, Any]) -> datetime | None:
    value = _text(metadata, "localDateTime", "fileCreatedAt", "dateTimeOriginal")
    exif = metadata.get("exifInfo")
    if not value and isinstance(exif, dict):
        value = _text(exif, "dateTimeOriginal", "modifyDate")
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
    make = _text(exif, "make").lower()
    model = _text(exif, "model").lower()
    value = f"{make}:{model}".strip(":")
    return value or "unknown"


def _stack_key(metadata: dict[str, Any]) -> str | None:
    for key in ("stackParentId", "stackId"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def ensure_collection_context_snapshot(
    session: AsyncSession,
    *,
    asset: Asset,
    revision: AssetRevision,
) -> CollectionContextSnapshot:
    existing = await session.scalar(
        select(CollectionContextSnapshot).where(
            CollectionContextSnapshot.asset_id == asset.id,
            CollectionContextSnapshot.asset_revision_id == revision.id,
            CollectionContextSnapshot.processor_version == COLLECTION_CONTEXT_PROCESSOR_VERSION,
        )
    )
    if existing is not None:
        return existing
    # nosemgrep: len-all-count -- a small per-asset list, not a table scan
    face_count = len(
        (
            await session.scalars(
                select(AssetFaceReference.id).where(
                    AssetFaceReference.asset_id == asset.id,
                    AssetFaceReference.access_state == "available",
                    AssetFaceReference.is_visible.is_(True),
                )
            )
        ).all()
    )
    metadata = revision.normalized_metadata
    captured_at = _capture_time(metadata)
    exif = metadata.get("exifInfo")
    exif_values = exif if isinstance(exif, dict) else {}
    facts: dict[str, object] = {
        "source": "immich_metadata",
        "captured_at": captured_at.isoformat() if captured_at else None,
        "camera_key": _camera_key(metadata),
        "stack_reference": _stack_key(metadata),
        "visible_immich_face_references": face_count,
        "place": {
            "city": exif_values.get("city"),
            "state": exif_values.get("state"),
            "country": exif_values.get("country"),
        },
    }
    limitations = [
        "semantic_neighbors_not_loaded",
        "collection_uniqueness_not_proven",
    ]
    if captured_at is None:
        limitations.append("capture_time_unavailable")
    snapshot = CollectionContextSnapshot(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        processor_version=COLLECTION_CONTEXT_PROCESSOR_VERSION,
        status="partial",
        source_facts=facts,
        limitations=limitations,
        semantic_neighbors=[],
        evidence_digest=canonical_digest({"facts": facts, "limitations": limitations}),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


def _group_key(group_type: str, identity: str) -> str:
    return hashlib.sha256(f"{group_type}:{identity}".encode()).hexdigest()


async def _representative_asset_id(
    session: AsyncSession,
    members: list[tuple[Asset, AssetRevision, datetime]],
) -> uuid.UUID:
    snapshots = {
        snapshot.asset_id: snapshot
        for snapshot in (
            await session.scalars(
                select(AssetCurationSnapshot).where(
                    AssetCurationSnapshot.asset_id.in_(
                        [asset.id for asset, _revision, _capture in members]
                    )
                )
            )
        ).all()
    }

    def rank(row: tuple[Asset, AssetRevision, datetime]) -> tuple[object, ...]:
        asset, revision, captured = row
        snapshot = snapshots.get(asset.id)
        human = bool(snapshot and snapshot.rating_source in {"star_human", "immich_human"})
        return (
            human,
            revision.is_favorite,
            snapshot.effective_rating if snapshot and snapshot.effective_rating else 0,
            snapshot.rank_score if snapshot and snapshot.rank_score else -1,
            snapshot.confidence if snapshot else 0,
            (revision.width or 0) * (revision.height or 0),
            -captured.timestamp(),
            str(asset.id),
        )

    return max(members, key=rank)[0].id


async def _store_burst(
    session: AsyncSession,
    *,
    members: list[tuple[Asset, AssetRevision, datetime]],
    confidence: float,
    source: str,
) -> bool:
    if len(members) < 2:
        return False
    members.sort(key=lambda row: (row[2], str(row[0].id)))
    first_asset, first_revision, first_capture = members[0]
    last_capture = members[-1][2]
    camera_key = _camera_key(first_revision.normalized_metadata)
    stack_key = _stack_key(first_revision.normalized_metadata)
    gaps_ms = [
        max(0, round((current[2] - previous[2]).total_seconds() * 1000))
        for previous, current in pairwise(members)
    ]
    duration_ms = max(0, round((last_capture - first_capture).total_seconds() * 1000))
    maximum_gap_ms = max(gaps_ms, default=0)
    identity = (
        f"{first_asset.connection_id}:{source}:{stack_key}"
        if stack_key
        else f"{first_asset.connection_id}:{camera_key}:{first_capture.isoformat()}"
    )
    key = _group_key("burst", identity)
    overlapping_groups = list(
        (
            await session.scalars(
                select(CollectionGroup)
                .join(
                    CollectionGroupMember,
                    CollectionGroupMember.group_id == CollectionGroup.id,
                )
                .where(
                    CollectionGroup.group_type == "burst",
                    CollectionGroup.state == "active",
                    CollectionGroupMember.asset_id.in_(
                        [asset.id for asset, _revision, _capture in members]
                    ),
                    CollectionGroup.group_key != key,
                )
                .distinct()
            )
        ).all()
    )
    for overlapping in overlapping_groups:
        overlapping.state = "superseded"
    group = await session.scalar(
        select(CollectionGroup).where(
            CollectionGroup.group_type == "burst",
            CollectionGroup.group_key == key,
        )
    )
    if group is not None and group.state != "active":
        return False
    representative = await _representative_asset_id(session, members)
    if group is None:
        group = CollectionGroup(
            connection_id=first_asset.connection_id,
            group_type="burst",
            group_key=key,
            detection_version=BURST_DETECTION_VERSION,
            confidence=confidence,
            explanation=(
                "Immich stack members are shown as one burst."
                if source == "immich_stack"
                else (
                    "Consecutive captures from the same source and camera are shown "
                    "as a probable burst."
                )
            ),
            source_facts={
                "source": source,
                "camera_key": camera_key,
                "stack_reference": stack_key,
                "maximum_gap_ms": maximum_gap_ms,
                "duration_ms": duration_ms,
                "maximum_inferred_gap_ms": INFERRED_BURST_MAX_GAP_MS,
            },
            representative_asset_id=representative,
            captured_start_at=first_capture,
            captured_end_at=last_capture,
            member_count=len(members),
        )
        session.add(group)
        await session.flush()
    elif group.manual_representative_asset_id is None:
        group.representative_asset_id = representative
    group.detection_version = BURST_DETECTION_VERSION
    group.confidence = confidence
    group.source_facts = {
        "source": source,
        "camera_key": camera_key,
        "stack_reference": stack_key,
        "maximum_gap_ms": maximum_gap_ms,
        "duration_ms": duration_ms,
        "maximum_inferred_gap_ms": INFERRED_BURST_MAX_GAP_MS,
    }
    group.captured_start_at = first_capture
    group.captured_end_at = last_capture
    group.member_count = len(members)
    await session.execute(
        delete(CollectionGroupMember).where(CollectionGroupMember.group_id == group.id)
    )
    for index, (asset, revision, capture) in enumerate(members):
        session.add(
            CollectionGroupMember(
                group_id=group.id,
                asset_id=asset.id,
                asset_revision_id=revision.id,
                sequence_index=index,
                role="representative" if asset.id == representative else "member",
                confidence=confidence,
                source_facts={"captured_at": capture.isoformat()},
            )
        )
    await session.flush()
    return True


async def reconcile_collection_bursts(
    session: AsyncSession,
    *,
    assets: list[Asset],
) -> int:
    """Discover conservative burst groups inside one bounded admitted cohort."""

    rows: list[tuple[Asset, AssetRevision, datetime]] = []
    for asset in assets:
        if asset.current_revision_id is None:
            continue
        revision = await session.get(AssetRevision, asset.current_revision_id)
        if revision is None:
            continue
        captured = _capture_time(revision.normalized_metadata)
        if captured is not None:
            rows.append((asset, revision, captured))
    created = 0
    connection_ids = {asset.connection_id for asset in assets}
    if connection_ids:
        old_groups = list(
            (
                await session.scalars(
                    select(CollectionGroup).where(
                        CollectionGroup.connection_id.in_(connection_ids),
                        CollectionGroup.group_type == "burst",
                        CollectionGroup.state == "active",
                        CollectionGroup.detection_version != BURST_DETECTION_VERSION,
                    )
                )
            ).all()
        )
        for group in old_groups:
            if group.source_facts.get("source") == "capture_sequence":
                group.state = "superseded"
    stacks: defaultdict[tuple[uuid.UUID, str], list[tuple[Asset, AssetRevision, datetime]]] = (
        defaultdict(list)
    )
    ordinary: list[tuple[Asset, AssetRevision, datetime]] = []
    for row in rows:
        stack = _stack_key(row[1].normalized_metadata)
        if stack:
            stacks[(row[0].connection_id, stack)].append(row)
        else:
            ordinary.append(row)
    for members in stacks.values():
        if await _store_burst(
            session,
            members=members,
            confidence=0.98,
            source="immich_stack",
        ):
            created += 1

    ordinary.sort(
        key=lambda row: (
            str(row[0].connection_id),
            _camera_key(row[1].normalized_metadata),
            row[2],
            str(row[0].id),
        )
    )
    run: list[tuple[Asset, AssetRevision, datetime]] = []
    for row in ordinary:
        if not run:
            run = [row]
            continue
        previous = run[-1]
        camera = _camera_key(row[1].normalized_metadata)
        previous_camera = _camera_key(previous[1].normalized_metadata)
        same_source = row[0].connection_id == previous[0].connection_id
        same_camera = camera != "unknown" and camera == previous_camera
        gap = row[2] - previous[2]
        if (
            same_source
            and same_camera
            and timedelta(0) <= gap <= timedelta(milliseconds=INFERRED_BURST_MAX_GAP_MS)
        ):
            run.append(row)
            continue
        if len(run) >= INFERRED_BURST_MIN_FRAMES and await _store_burst(
            session,
            members=run,
            confidence=0.72,
            source="capture_sequence",
        ):
            created += 1
        run = [row]
    if len(run) >= INFERRED_BURST_MIN_FRAMES and await _store_burst(
        session,
        members=run,
        confidence=0.72,
        source="capture_sequence",
    ):
        created += 1
    return created


async def reconcile_collection_burst_backfill(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> int:
    """Revisit the admitted timeline in durable overlapping windows."""

    if limit < 1:
        return 0
    captured = func.coalesce(
        AssetRevision.normalized_metadata["localDateTime"].as_string(),
        AssetRevision.normalized_metadata["fileCreatedAt"].as_string(),
        AssetRevision.normalized_metadata["dateTimeOriginal"].as_string(),
        "",
    )
    marker = CollectionContextSnapshot.source_facts["burst_detection_version"].as_string()
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .outerjoin(
                    CollectionContextSnapshot,
                    CollectionContextSnapshot.asset_id == Asset.id,
                )
                .where(
                    Asset.media_type == "image",
                    Asset.access_state == "available",
                    AssetRevision.is_trashed.is_(False),
                    or_(
                        CollectionContextSnapshot.id.is_(None),
                        func.coalesce(marker, "") != BURST_DETECTION_VERSION,
                    ),
                )
                .order_by(captured, Asset.id)
                .limit(limit)
            )
        ).all()
    )
    if not assets:
        return 0
    await reconcile_collection_bursts(session, assets=assets)
    mark_count = (
        len(assets) if len(assets) < limit else max(1, len(assets) - BURST_BACKFILL_OVERLAP)
    )
    for asset in assets[:mark_count]:
        if asset.current_revision_id is None:
            continue
        revision = await session.get(AssetRevision, asset.current_revision_id)
        if revision is None:
            continue
        snapshot = await ensure_collection_context_snapshot(
            session,
            asset=asset,
            revision=revision,
        )
        snapshot.source_facts = {
            **snapshot.source_facts,
            "burst_detection_version": BURST_DETECTION_VERSION,
        }
    await session.flush()
    return mark_count
