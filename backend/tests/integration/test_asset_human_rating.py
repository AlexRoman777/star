from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    AuditEvent,
    CleanupCampaign,
    HumanDecision,
    ImmichConnection,
    ImmichInstance,
    Proposal,
)
from star.domain.enums import (
    AccountPurpose,
    HealthStatus,
    MediaType,
    ProposalState,
    ProposalType,
)
from star.main import app
from star.services.owner_labels import current_human_rating_labels


async def _analyzed_asset(
    session: AsyncSession,
    actor_id: uuid.UUID,
) -> tuple[Asset, AssetRevision, CleanupCampaign]:
    instance = ImmichInstance(
        stable_instance_id="rating-immich",
        base_url="http://immich.local",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="camera-owner",
        display_label="Camera",
        purpose=AccountPurpose.NIKON,
        processing_profile="camera",
        model_profile="camera-default",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="rating-photo",
        immich_owner_user_id="camera-owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="d" * 64,
        normalized_metadata={"originalFileName": "rating-photo.jpg"},
        rating=4,
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    campaign = CleanupCampaign(
        origin="background",
        name="Existing background analysis",
        objective="Analyze the photo without requiring feedback",
        scope={"asset_ids": [str(asset.id)]},
        created_by=actor_id,
    )
    session.add(campaign)
    await session.flush()
    session.add(
        AnalysisRun(
            campaign_id=campaign.id,
            asset_id=asset.id,
            asset_revision_id=revision.id,
            analysis_type="technical_preview",
            processor_version="rating-test-v1",
            model_key="deterministic",
            status="succeeded",
            completed_at=utc_now(),
        )
    )
    session.add(
        Proposal(
            campaign_id=campaign.id,
            proposal_type=ProposalType.RATING,
            target_type="asset",
            target_id=asset.id,
            value={"rating": 2, "source": "technical_fullsize"},
            confidence=0.7,
            reason_codes=["technical_test"],
            explanation="A retained model vote",
            state=ProposalState.PENDING,
            analysis_version="rating-test-v1",
        )
    )
    await session.commit()
    return asset, revision, campaign


async def test_asset_rating_is_versioned_audited_local_feedback(
    authenticated_context,
    session_factory,
) -> None:
    async with session_factory() as session:
        asset, revision, campaign = await _analyzed_asset(
            session,
            authenticated_context["user_id"],
        )
        asset_id = asset.id
        revision_id = revision.id
        campaign_id = campaign.id

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        first = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": 5,
                "expected_decision_id": None,
                "idempotency_key": "asset-rating-first",
            },
        )
        assert first.status_code == 200, first.text
        first_payload = first.json()
        first_id = first_payload["human_rating"]["decision_id"]
        assert first_payload["human_rating"]["rating"] == 5
        assert first_payload["human_rating"]["immich_unchanged"] is True
        assert first_payload["curation"]["effective_rating"] == 5
        assert first_payload["curation"]["rating_source"] == "star_human"
        assert first_payload["curation"]["review_priority"] == 0

        async with session_factory() as session:
            snapshot = await session.get(AssetCurationSnapshot, asset_id)
            assert snapshot is not None
            snapshot.analysis_complete = True
            snapshot.description = "A completed family portrait with warm evening color"
            snapshot.final_summary = "Strong family value and a natural candid moment"
            await session.commit()
            labels = await current_human_rating_labels(session)
            assert len(labels) == 1
            assert labels[0].asset_id == asset_id
            assert labels[0].rating == 5

        second = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": 2,
                "reason": "Nice colors, but weak family value",
                "keywords": ["nice colors", "family value"],
                "expected_decision_id": first_id,
                "idempotency_key": "asset-rating-second",
            },
        )
        assert second.status_code == 200, second.text
        second_payload = second.json()
        second_id = second_payload["human_rating"]["decision_id"]
        assert second_payload["human_rating"]["supersedes_id"] == first_id
        assert second_payload["human_rating"]["reason"] == "Nice colors, but weak family value"
        assert second_payload["human_rating"]["keywords"] == ["nice colors", "family value"]
        assert second_payload["curation"]["effective_rating"] == 2
        context_update = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": 2,
                "reason": "Family value matters more than perfect sharpness",
                "keywords": ["family value", "candid"],
                "expected_decision_id": second_id,
                "idempotency_key": "asset-rating-context-update",
            },
        )
        assert context_update.status_code == 200, context_update.text
        context_payload = context_update.json()
        context_id = context_payload["human_rating"]["decision_id"]
        assert context_id != second_id
        assert context_payload["human_rating"]["rating"] == 2
        assert context_payload["human_rating"]["keywords"] == ["family value", "candid"]
        assert context_payload["validation_task_id"] is None
        owner_gallery = await client.get(
            "/api/v1/assets/gallery?human_rated=true&grouping=individual&limit=12"
        )
        assert owner_gallery.status_code == 200, owner_gallery.text
        assert owner_gallery.json()["total"] == 1
        assert owner_gallery.json()["items"][0]["id"] == str(asset_id)

        stale = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": 3,
                "expected_decision_id": first_id,
                "idempotency_key": "asset-rating-stale",
            },
        )
        assert stale.status_code == 409

        cleared = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": None,
                "expected_decision_id": context_id,
                "idempotency_key": "asset-rating-clear",
            },
        )
        assert cleared.status_code == 200, cleared.text
        cleared_payload = cleared.json()
        assert cleared_payload["human_rating"]["rating"] is None
        assert cleared_payload["human_rating"]["supersedes_id"] == context_id
        assert cleared_payload["curation"]["effective_rating"] == 4
        assert cleared_payload["curation"]["rating_source"] == "immich_human"
        cleared_owner_gallery = await client.get(
            "/api/v1/assets/gallery?human_rated=true&grouping=individual&limit=12"
        )
        assert cleared_owner_gallery.status_code == 200, cleared_owner_gallery.text
        assert cleared_owner_gallery.json()["total"] == 0

        repeated = await client.put(
            f"/api/v1/assets/{asset_id}/human-rating",
            json={
                "rating": None,
                "expected_decision_id": second_id,
                "idempotency_key": "asset-rating-clear",
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert (
            repeated.json()["human_rating"]["decision_id"]
            == cleared_payload["human_rating"]["decision_id"]
        )

        inspector = await client.get(f"/api/v1/assets/{asset_id}/inspector")
        gallery = await client.get("/api/v1/assets/gallery?limit=12&rating_min=4&rating_max=5")
        weak_gallery = await client.get("/api/v1/assets/gallery?limit=12&rating_min=1&rating_max=2")
        disagreement_gallery = await client.get(
            "/api/v1/assets/gallery?limit=12&vote_count_min=2&agreement_max=0.7"
        )

    assert inspector.status_code == 200, inspector.text
    assert inspector.json()["human_rating"]["rating"] is None
    assert inspector.json()["curation"]["effective_rating"] == 4
    assert gallery.status_code == 200, gallery.text
    gallery_item = gallery.json()["items"][0]
    assert gallery_item["effective_rating"] == 4
    assert gallery_item["rating_source"] == "immich_human"
    assert weak_gallery.status_code == 200, weak_gallery.text
    assert weak_gallery.json()["total"] == 0
    assert disagreement_gallery.status_code == 200, disagreement_gallery.text
    assert disagreement_gallery.json()["total"] == 0

    async with session_factory() as session:
        stored_revision = await session.get(AssetRevision, revision_id)
        assert stored_revision is not None
        assert stored_revision.rating == 4
        decisions = list(
            (
                await session.scalars(
                    select(HumanDecision)
                    .where(HumanDecision.target_type == "asset")
                    .order_by(HumanDecision.expected_revision)
                )
            ).all()
        )
        assert len(decisions) == 4
        assert all(decision.campaign_id == campaign_id for decision in decisions)
        assert decisions[1].supersedes_id == decisions[0].id
        assert decisions[2].supersedes_id == decisions[1].id
        assert decisions[3].supersedes_id == decisions[2].id
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 4
        assert await current_human_rating_labels(session) == []
