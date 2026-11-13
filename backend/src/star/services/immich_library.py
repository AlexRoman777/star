from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from typing import Any

import httpx
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.crypto import SecretCipher
from star.db.models import (
    AlbumReference,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetRevision,
    CleanupCampaign,
    ImmichConnection,
    ImmichInstance,
    LibraryProgram,
    PersonReference,
    Proposal,
    SimilarityGroup,
    SimilarityGroupMember,
    TagReference,
    Task,
    WritebackOperation,
)
from star.domain.enums import (
    CandidateBreadth,
    CredentialKind,
    GroupType,
    MediaType,
    ProposalState,
    ProposalType,
    RawPolicy,
    WritebackState,
)
from star.domain.schemas import (
    CampaignCreate,
    ImmichLibraryMetricsResponse,
    ImmichLibraryOverviewResponse,
    RandomPhotoSampleAssetResponse,
    RandomPhotoSampleResponse,
    StarLibraryMetricsResponse,
)
from star.immich.client import ImmichApiError, ImmichClient
from star.services.campaigns import create_campaign, start_campaign
from star.services.connections import get_active_credential
from star.services.sync import _upsert_asset_event

SAMPLE_ORIGIN = "random_sample"
DUPLICATE_GROUP_TYPES = {
    GroupType.EXACT_BINARY,
    GroupType.SAME_DECODED,
    GroupType.RESIZED,
    GroupType.RECOMPRESSED,
}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def _optional_immich_read(
    name: str,
    operation: Awaitable[Any],
) -> tuple[str, Any | None, str | None]:
    try:
        return name, await operation, None
    except ImmichApiError as exc:
        code = f"immich_http_{exc.status_code}" if exc.status_code else "unexpected_response"
        return name, None, code
    except httpx.HTTPError:
        return name, None, "immich_unreachable"
    except ValueError:
        return name, None, "unexpected_response"


def _unavailable_immich_metrics() -> ImmichLibraryMetricsResponse:
    return ImmichLibraryMetricsResponse(
        total_assets=None,
        photos=None,
        videos=None,
        favorites=None,
        archived=None,
        trashed=None,
        people=None,
        hidden_people=None,
        albums=None,
        album_memberships=None,
        tags=None,
    )


def _unavailable_star_metrics() -> StarLibraryMetricsResponse:
    return StarLibraryMetricsResponse(
        imported_assets=None,
        imported_photos=None,
        imported_videos=None,
        asset_revisions=None,
        analyzed_photos=None,
        completed_reviews=None,
        ratings_written=None,
        tags_written=None,
        writeback_conflicts=None,
        known_people=None,
        known_faces=None,
        geotagged_assets=None,
        albums=None,
        tags=None,
        similarity_groups=None,
        duplicate_groups=None,
    )


def _metadata_text(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _connection_client_values(
    session: AsyncSession,
    connection: ImmichConnection,
    cipher: SecretCipher,
) -> tuple[ImmichInstance, str]:
    instance = await session.get(ImmichInstance, connection.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=connection.id,
        kind=CredentialKind.READ,
    )
    if instance is None or credential is None:
        raise ValueError("connection is missing its instance or read credential")
    return instance, cipher.decrypt(credential.encrypted_secret)


async def refresh_immich_photo_population(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    program: LibraryProgram,
    cipher: SecretCipher,
) -> bool:
    """Refresh the dynamic point ceiling from Immich without ingesting media."""

    connection_id = connection.id
    program_id = program.id
    instance, api_key = await _connection_client_values(session, connection, cipher)
    instance_id = instance.id
    base_url = instance.base_url
    await session.commit()
    async with ImmichClient(base_url, api_key) as client:
        statistics = await client.get_asset_statistics()
    photo_count = _integer(statistics.get("images"))
    locked_program = await session.scalar(
        select(LibraryProgram)
        .where(LibraryProgram.id == program_id)
        .with_for_update(skip_locked=True)
    )
    if locked_program is None:
        return False
    locked_connection = await session.scalar(
        select(ImmichConnection)
        .where(ImmichConnection.id == connection_id)
        .with_for_update(skip_locked=True)
    )
    locked_instance = await session.scalar(
        select(ImmichInstance)
        .where(ImmichInstance.id == instance_id)
        .with_for_update(skip_locked=True)
    )
    if locked_connection is None or locked_instance is None:
        await session.commit()
        return False
    if (
        locked_connection is None
        or locked_connection.instance_id != instance_id
        or locked_program is None
        or locked_instance is None
        or locked_instance.base_url != base_url
    ):
        raise ValueError("Immich source changed during the population refresh")
    if photo_count is None or photo_count < 0 or photo_count == locked_program.source_total_count:
        return False
    locked_program.source_total_count = photo_count
    return True


async def _count(session: AsyncSession, statement: Any) -> int:
    return int(await session.scalar(statement) or 0)


async def _local_library_metrics(
    session: AsyncSession,
    connection: ImmichConnection,
) -> StarLibraryMetricsResponse:
    assets_for_connection = Asset.connection_id == connection.id
    imported_assets = await _count(
        session,
        select(func.count()).select_from(Asset).where(assets_for_connection),
    )
    imported_photos = await _count(
        session,
        select(func.count())
        .select_from(Asset)
        .where(assets_for_connection, Asset.media_type == MediaType.IMAGE),
    )
    imported_videos = await _count(
        session,
        select(func.count())
        .select_from(Asset)
        .where(assets_for_connection, Asset.media_type == MediaType.VIDEO),
    )
    asset_revisions = await _count(
        session,
        select(func.count())
        .select_from(AssetRevision)
        .join(Asset, Asset.id == AssetRevision.asset_id)
        .where(assets_for_connection),
    )
    analyzed_photos = await _count(
        session,
        select(func.count(distinct(AnalysisRun.asset_id)))
        .select_from(AnalysisRun)
        .join(Asset, Asset.id == AnalysisRun.asset_id)
        .where(
            assets_for_connection,
            AnalysisRun.analysis_type == "technical_preview",
            AnalysisRun.status == "succeeded",
        ),
    )
    completed_reviews = await _count(
        session,
        select(func.count())
        .select_from(AssetCurationSnapshot)
        .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
        .where(assets_for_connection, AssetCurationSnapshot.analysis_complete.is_(True)),
    )
    ratings_written = await _count(
        session,
        select(func.count())
        .select_from(WritebackOperation)
        .where(
            WritebackOperation.connection_id == connection.id,
            WritebackOperation.operation_type == "rating",
            WritebackOperation.state == WritebackState.SUCCEEDED,
        ),
    )
    tags_written = await _count(
        session,
        select(func.count())
        .select_from(WritebackOperation)
        .where(
            WritebackOperation.connection_id == connection.id,
            WritebackOperation.operation_type == "tag",
            WritebackOperation.state == WritebackState.SUCCEEDED,
        ),
    )
    writeback_conflicts = await _count(
        session,
        select(func.count())
        .select_from(WritebackOperation)
        .where(
            WritebackOperation.connection_id == connection.id,
            WritebackOperation.state == WritebackState.CONFLICT,
        ),
    )
    known_people = await _count(
        session,
        select(func.count())
        .select_from(PersonReference)
        .where(
            PersonReference.connection_id == connection.id,
            PersonReference.access_state == "available",
        ),
    )
    known_faces = await _count(
        session,
        select(func.count())
        .select_from(AssetFaceReference)
        .where(
            AssetFaceReference.connection_id == connection.id,
            AssetFaceReference.access_state == "available",
        ),
    )
    latitude = AssetRevision.normalized_metadata["exifInfo"]["latitude"].as_string()
    longitude = AssetRevision.normalized_metadata["exifInfo"]["longitude"].as_string()
    geotagged_assets = await _count(
        session,
        select(func.count(distinct(Asset.id)))
        .select_from(Asset)
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .where(assets_for_connection, or_(latitude.is_not(None), longitude.is_not(None))),
    )
    albums = await _count(
        session,
        select(func.count())
        .select_from(AlbumReference)
        .where(
            AlbumReference.connection_id == connection.id,
            AlbumReference.access_state == "available",
        ),
    )
    tags = await _count(
        session,
        select(func.count())
        .select_from(TagReference)
        .where(
            TagReference.connection_id == connection.id,
            TagReference.access_state == "available",
        ),
    )
    similarity_base = (
        select(func.count(distinct(SimilarityGroup.id)))
        .select_from(SimilarityGroup)
        .join(
            SimilarityGroupMember,
            SimilarityGroupMember.group_id == SimilarityGroup.id,
        )
        .join(Asset, Asset.id == SimilarityGroupMember.asset_id)
        .where(assets_for_connection)
    )
    similarity_groups = await _count(session, similarity_base)
    duplicate_groups = await _count(
        session,
        similarity_base.where(SimilarityGroup.group_type.in_(DUPLICATE_GROUP_TYPES)),
    )
    return StarLibraryMetricsResponse(
        imported_assets=imported_assets,
        imported_photos=imported_photos,
        imported_videos=imported_videos,
        asset_revisions=asset_revisions,
        analyzed_photos=analyzed_photos,
        completed_reviews=completed_reviews,
        ratings_written=ratings_written,
        tags_written=tags_written,
        writeback_conflicts=writeback_conflicts,
        known_people=known_people,
        known_faces=known_faces,
        geotagged_assets=geotagged_assets,
        albums=albums,
        tags=tags,
        similarity_groups=similarity_groups,
        duplicate_groups=duplicate_groups,
    )


async def get_library_overview(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    cipher: SecretCipher,
) -> ImmichLibraryOverviewResponse:
    instance, api_key = await _connection_client_values(session, connection, cipher)
    await session.commit()
    unavailable: dict[str, str] = {}
    async with ImmichClient(instance.base_url, api_key) as client:
        reads = await asyncio.gather(
            _optional_immich_read("assets", client.get_asset_statistics()),
            _optional_immich_read(
                "favorites",
                client.get_asset_statistics(is_favorite=True),
            ),
            _optional_immich_read(
                "archive",
                client.get_asset_statistics(visibility="archive"),
            ),
            _optional_immich_read("trash", client.get_asset_statistics(is_trashed=True)),
            _optional_immich_read("people", client.get_people_overview()),
            _optional_immich_read("albums", client.get_albums()),
            _optional_immich_read("tags", client.get_tags()),
        )
    values: dict[str, Any] = {}
    for name, value, error_code in reads:
        values[name] = value
        if error_code is not None:
            unavailable[f"immich.{name}"] = error_code
    immich = _unavailable_immich_metrics()
    stats = values.get("assets")
    if isinstance(stats, dict):
        immich.total_assets = _integer(stats.get("total"))
        immich.photos = _integer(stats.get("images"))
        immich.videos = _integer(stats.get("videos"))
    statistic_fields = (
        ("favorites", "favorites"),
        ("archive", "archived"),
        ("trash", "trashed"),
    )
    for name, field in statistic_fields:
        metric = values.get(name)
        if isinstance(metric, dict):
            setattr(immich, field, _integer(metric.get("total")))
    people = values.get("people")
    if isinstance(people, dict):
        immich.people = _integer(people.get("total"))
        immich.hidden_people = _integer(people.get("hidden"))
    albums = values.get("albums")
    if isinstance(albums, list):
        immich.albums = len(albums)
        immich.album_memberships = sum(
            _integer(item.get("assetCount")) for item in albums if isinstance(item, dict)
        )
    tags = values.get("tags")
    if isinstance(tags, list):
        immich.tags = len(tags)
    try:
        star = await _local_library_metrics(session, connection)
    except SQLAlchemyError:
        star = _unavailable_star_metrics()
        unavailable["star.index"] = "database_query_failed"
    return ImmichLibraryOverviewResponse(
        connection_id=connection.id,
        immich_server_version=instance.server_version or "unknown",
        immich=immich,
        star=star,
        unavailable_metrics=unavailable,
    )


def _safe_sample_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"owner", "people", "originalPath"}
    }


async def create_random_photo_sample(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    cipher: SecretCipher,
    actor_id: uuid.UUID,
    size: int,
    start_analysis: bool,
) -> CleanupCampaign:
    connection_id = connection.id
    instance, api_key = await _connection_client_values(session, connection, cipher)
    instance_id = instance.id
    base_url = instance.base_url
    await session.commit()
    async with ImmichClient(base_url, api_key) as client:
        snapshots = await client.search_random_photos(size)
    locked_connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    locked_instance = await session.scalar(
        select(ImmichInstance).where(ImmichInstance.id == instance_id).with_for_update()
    )
    if (
        locked_connection is None
        or locked_connection.instance_id != instance_id
        or locked_instance is None
        or locked_instance.base_url != base_url
    ):
        raise ValueError("Immich source changed during random photo selection")
    asset_ids: list[uuid.UUID] = []
    observed_immich_ids: set[str] = set()
    for snapshot in snapshots:
        immich_asset_id = str(snapshot.get("id") or "")
        if not immich_asset_id or immich_asset_id in observed_immich_ids:
            continue
        if str(snapshot.get("type") or "").lower() not in {"image", "photo"}:
            continue
        observed_immich_ids.add(immich_asset_id)
        await _upsert_asset_event(
            session,
            instance_id=locked_instance.id,
            connection=locked_connection,
            payload=_safe_sample_payload(snapshot),
        )
        asset = await session.scalar(
            select(Asset).where(
                Asset.instance_id == locked_instance.id,
                Asset.connection_id == locked_connection.id,
                Asset.immich_asset_id == immich_asset_id,
            )
        )
        if asset is not None:
            asset_ids.append(asset.id)
    if not asset_ids:
        raise ValueError("Immich returned no eligible photos for the random sample")
    campaign = await create_campaign(
        session,
        payload=CampaignCreate(
            name=f"Random {locked_connection.display_label} sample · {len(asset_ids)} photos",
            objective="Test Star's thumbnail-first technical analysis on a random sample",
            connection_ids=[locked_connection.id],
            scope={
                "asset_ids": [str(asset_id) for asset_id in asset_ids],
                "sample_kind": "random_photos",
                "requested_size": size,
            },
            candidate_breadth=CandidateBreadth.BALANCED,
            raw_policy=RawPolicy.STRONGLY_PROTECTED,
            automatic_tags=[],
        ),
        actor_id=actor_id,
    )
    campaign.origin = SAMPLE_ORIGIN
    if start_analysis:
        await start_campaign(session, campaign)
    return campaign


def _sample_asset_response(
    asset: Asset,
    revision: AssetRevision,
    task_by_asset: dict[uuid.UUID, Task],
    run_by_asset: dict[uuid.UUID, AnalysisRun],
    proposal_by_asset: dict[uuid.UUID, Proposal],
) -> RandomPhotoSampleAssetResponse:
    run = run_by_asset.get(asset.id)
    task = task_by_asset.get(asset.id)
    if run is not None:
        analysis_state = "analyzed" if run.status == "succeeded" else run.status
    elif task is not None:
        analysis_state = task.state.value
    else:
        analysis_state = "waiting"
    metadata = revision.normalized_metadata
    exif_value = metadata.get("exifInfo")
    exif: dict[str, Any] = {}
    if isinstance(exif_value, dict):
        exif = {str(key): value for key, value in exif_value.items()}
    make = _metadata_text(exif, "make")
    model = _metadata_text(exif, "model")
    camera = " ".join(value for value in (make, model) if value) or None
    proposal = proposal_by_asset.get(asset.id)
    proposed_rating = None
    if proposal is not None:
        rating = proposal.value.get("rating")
        proposed_rating = rating if isinstance(rating, int) else None
    return RandomPhotoSampleAssetResponse(
        id=asset.id,
        immich_asset_id=asset.immich_asset_id,
        filename=_metadata_text(metadata, "originalFileName", "fileName") or "Photo",
        captured_at=_metadata_text(metadata, "localDateTime", "fileCreatedAt", "createdAt"),
        camera=camera,
        width=revision.width,
        height=revision.height,
        existing_rating=revision.rating,
        analysis_state=analysis_state,
        proposed_rating=proposed_rating,
        proposal_confidence=proposal.confidence if proposal else None,
    )


async def random_sample_response(
    session: AsyncSession,
    campaign: CleanupCampaign,
) -> RandomPhotoSampleResponse:
    raw_asset_ids = campaign.scope.get("asset_ids", [])
    asset_ids: list[uuid.UUID] = []
    for value in raw_asset_ids:
        try:
            asset_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not asset_ids:
        raise ValueError("random sample has no valid assets")
    rows = (
        await session.execute(
            select(Asset, AssetRevision)
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(Asset.id.in_(asset_ids))
        )
    ).all()
    assets_by_id = {asset.id: (asset, revision) for asset, revision in rows}
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.campaign_id == campaign.id, Task.asset_id.in_(asset_ids))
                .order_by(Task.created_at.desc())
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(AnalysisRun)
                .join(Asset, Asset.id == AnalysisRun.asset_id)
                .where(
                    AnalysisRun.asset_id.in_(asset_ids),
                    or_(
                        Asset.current_generation_id.is_(None),
                        AnalysisRun.analysis_generation_id == Asset.current_generation_id,
                    ),
                    AnalysisRun.analysis_type == "technical_preview",
                )
                .order_by(AnalysisRun.started_at.desc())
            )
        ).all()
    )
    proposals = list(
        (
            await session.scalars(
                select(Proposal)
                .join(Asset, Asset.id == Proposal.target_id)
                .where(
                    Proposal.target_id.in_(asset_ids),
                    Proposal.target_type == "asset",
                    Proposal.proposal_type == ProposalType.RATING,
                    Proposal.state == ProposalState.PENDING,
                    or_(
                        Asset.current_generation_id.is_(None),
                        Proposal.analysis_generation_id == Asset.current_generation_id,
                    ),
                )
                .order_by(Proposal.created_at.desc())
            )
        ).all()
    )
    task_by_asset: dict[uuid.UUID, Task] = {}
    run_by_asset: dict[uuid.UUID, AnalysisRun] = {}
    proposal_by_asset: dict[uuid.UUID, Proposal] = {}
    for task in tasks:
        if task.asset_id is not None:
            task_by_asset.setdefault(task.asset_id, task)
    for run in runs:
        run_by_asset.setdefault(run.asset_id, run)
    for proposal in proposals:
        proposal_by_asset.setdefault(proposal.target_id, proposal)
    assets: list[RandomPhotoSampleAssetResponse] = []
    for asset_id in asset_ids:
        values = assets_by_id.get(asset_id)
        if values is None:
            continue
        asset, revision = values
        assets.append(
            _sample_asset_response(
                asset,
                revision,
                task_by_asset,
                run_by_asset,
                proposal_by_asset,
            )
        )
    analysis_counts: dict[str, int] = {}
    for asset in assets:
        analysis_counts[asset.analysis_state] = analysis_counts.get(asset.analysis_state, 0) + 1
    connection_values = campaign.scope.get("connection_ids", [])
    if not connection_values:
        raise ValueError("random sample has no connection")
    return RandomPhotoSampleResponse(
        campaign_id=campaign.id,
        connection_id=uuid.UUID(str(connection_values[0])),
        requested_size=_integer(campaign.scope.get("requested_size")),
        actual_size=len(assets),
        state=campaign.state,
        analysis_counts=analysis_counts,
        assets=assets,
        created_at=campaign.created_at,
    )


async def latest_random_sample(
    session: AsyncSession,
    connection_id: uuid.UUID,
) -> RandomPhotoSampleResponse | None:
    campaigns = list(
        (
            await session.scalars(
                select(CleanupCampaign)
                .where(CleanupCampaign.origin == SAMPLE_ORIGIN)
                .order_by(CleanupCampaign.created_at.desc())
                .limit(50)
            )
        ).all()
    )
    for campaign in campaigns:
        if str(connection_id) in {str(value) for value in campaign.scope.get("connection_ids", [])}:
            return await random_sample_response(session, campaign)
    return None
