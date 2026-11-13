"""A re-review wave must walk the library forward, never in circles.

The stale wave scanned 12,500 photographs and published 210. Its cursor sat on
one timestamp for a quarter of an hour while every pass re-selected the same
chunk, created fresh generations for photographs that already had one in
flight, and cancelled the work behind them. The machines ran flat out and the
library never moved.
"""

from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    ReviewReanalysisRun,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import AccountPurpose, HealthStatus, MediaType
from star.services.review_prompts import REANALYSIS_CHUNK_SIZE, _reanalysis_asset_statement


async def test_the_cursor_walks_photographs_that_share_one_ingest_moment(session) -> None:
    """Photographs bulk-imported in one moment share a created_at to the microsecond.

    The whole selection then rests on the identity tiebreak, so it has to order
    exactly the way the query sorts. Comparing a text cast of the identifier
    against a text identifier is not that ordering, and the wave walked the
    same chunk forever.
    """

    instance = ImmichInstance(
        stable_instance_id="cursor-walk",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()

    ingested_at = utc_now()
    total = 12
    for index in range(total):
        asset = Asset(
            instance_id=instance.id,
            connection_id=connection.id,
            immich_asset_id=f"photo-{index}",
            immich_owner_user_id="owner",
            media_type=MediaType.IMAGE,
            created_at=ingested_at,
        )
        session.add(asset)
        await session.flush()
        revision = AssetRevision(
            asset_id=asset.id,
            immich_updated_at=ingested_at,
            metadata_digest=f"{index:064d}",
            normalized_metadata={},
        )
        session.add(revision)
        await session.flush()
        asset.current_revision_id = revision.id
    await session.flush()

    run = ReviewReanalysisRun(
        program_id=uuid.uuid4(),
        review_generation=1,
        scope="analyzed",
        selection={},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="continuous_contract_refresh",
        state="running",
        selection_cutoff=ingested_at,
    )
    # "analyzed" would need review history; walk every photograph instead.
    run.scope = "all"
    session.add(run)
    await session.flush()

    seen: list[uuid.UUID] = []
    for _ in range(total * 2):
        page = _reanalysis_asset_statement(run).limit(min(4, REANALYSIS_CHUNK_SIZE))
        chunk = list(await session.scalars(page))
        if not chunk:
            break
        for asset in chunk:
            run.cursor_created_at = asset.created_at
            run.cursor_asset_id = asset.id
            seen.append(asset.id)
        await session.flush()

    assert len(seen) == total, "the wave must visit every photograph"
    assert len(set(seen)) == total, "and must never hand the same photograph back twice"


async def test_a_frozen_membership_still_walks_forward(session) -> None:
    """Every real run freezes its membership, so this is the branch that matters.

    The frozen branch used to return before the cursor was applied. The cursor
    was written on every pass and never read, so the wave re-selected its first
    chunk forever: 114,000 scans, 263 published, 141,000 cancelled tasks. The
    earlier test walked the unfrozen branch and passed against that code.
    """

    instance = ImmichInstance(
        stable_instance_id="frozen-walk",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()

    ingested_at = utc_now()
    run = ReviewReanalysisRun(
        program_id=uuid.uuid4(),
        review_generation=1,
        scope="stale",
        selection={"_membership_frozen": True, "_requested_photo_count": 9},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="continuous_contract_refresh",
        state="running",
        selection_cutoff=ingested_at,
    )
    session.add(run)
    await session.flush()
    session.add(
        WorkRun(
            id=run.id,
            kind="reanalysis",
            program_id=run.program_id,
            library_generation=1,
            requested=9,
            snapshot={},
            created_at=ingested_at,
        )
    )
    total = 9
    for index in range(total):
        asset = Asset(
            instance_id=instance.id,
            connection_id=connection.id,
            immich_asset_id=f"member-{index}",
            immich_owner_user_id="owner",
            media_type=MediaType.IMAGE,
            created_at=ingested_at,
        )
        session.add(asset)
        await session.flush()
        revision = AssetRevision(
            asset_id=asset.id,
            immich_updated_at=ingested_at,
            metadata_digest=f"{index:064d}",
            normalized_metadata={},
        )
        session.add(revision)
        await session.flush()
        asset.current_revision_id = revision.id
        session.add(WorkRunAsset(run_id=run.id, asset_id=asset.id, asset_revision_id=revision.id))
    await session.flush()

    seen: list[uuid.UUID] = []
    for _ in range(total * 2):
        page = _reanalysis_asset_statement(run).limit(3)
        chunk = list(await session.scalars(page))
        if not chunk:
            break
        for asset in chunk:
            run.cursor_created_at = asset.created_at
            run.cursor_asset_id = asset.id
            seen.append(asset.id)
        await session.flush()

    assert len(seen) == total
    assert len(set(seen)) == total, "the frozen membership must never hand a photograph back twice"
