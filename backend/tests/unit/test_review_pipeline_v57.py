from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from conftest import media_provenance, photo_observation_document
from star.analysis.review_pipeline import (
    APPLE_ADVISORY_MODEL_KEY,
    REVIEW_CONTRACT_VERSION,
    route_primary_review,
)
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    CleanupCampaign,
    HighlightLeagueEntry,
    LibraryProgram,
    ModelVersion,
    Proposal,
    Task,
    User,
    VisualFingerprint,
)
from star.domain.enums import (
    MediaType,
    ProposalType,
    Role,
    TaskState,
)
from star.services.analysis_generations import (
    advance_analysis_generations,
    create_analysis_generation,
)
from star.services.highlight_league import (
    HIGHLIGHT_GENERATION,
    HUMAN_ANCHOR_REASON,
    STATE_ORDERED,
    _comparison_candidates,
    _finalize_highlight_order,
)


async def _review_subject(session, *, with_qwen: bool = True):
    owner = User(
        username=f"review-owner-{uuid.uuid4()}",
        display_name="Review Owner",
        role=Role.ADMINISTRATOR,
    )
    program = LibraryProgram(
        name=f"Review program {uuid.uuid4()}",
        revision=7,
        deep_review_min_rating=4,
        deep_review_exceptions={
            "privacy_ambiguity": True,
            "protected_three_star": True,
            "ordinary_three_star": False,
        },
        model_pipeline_policy={
            "review_contract": REVIEW_CONTRACT_VERSION,
            "nvidia_primary_model": "ministral-3:8b-instruct-2512-q4_K_M",
            "apple_advisory_model": APPLE_ADVISORY_MODEL_KEY,
            "audit_sample_percent": 0,
            "discretionary_queue_limit": 100,
        },
    )
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=f"review-asset-{uuid.uuid4()}",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add_all([owner, program, asset])
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest=f"review-revision-{uuid.uuid4()}",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="automatic_review",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"kind": "test"},
    )
    technical_run = AnalysisRun(
        library_program_id=program.id,
        analysis_generation_id=generation.id,
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_type="technical_preview",
        processor_version="test",
        status="succeeded",
        completed_at=datetime.now(UTC),
    )
    session.add(technical_run)
    await session.flush()
    session.add(
        VisualFingerprint(
            asset_id=asset.id,
            asset_revision_id=revision.id,
            analysis_run_id=technical_run.id,
            algorithm="phash",
            algorithm_version="imagehash-4.3.2-hash16",
            value="0" * 64,
            bit_length=256,
            similarity_checked_at=datetime.now(UTC),
        )
    )
    campaign = CleanupCampaign(
        library_program_id=program.id,
        name="generation review",
        objective="test adaptive final routing",
        scope={"asset_ids": [str(asset.id)]},
        created_by=owner.id,
    )
    session.add(campaign)
    if with_qwen:
        session.add(
            ModelVersion(
                model_id=APPLE_ADVISORY_MODEL_KEY,
                version=str(uuid.uuid4()),
                runtime="mlx",
                quantization="Q4",
                license_id="Apache-2.0",
                artifact_checksum="sha256:" + "a" * 64,
                capabilities={
                    "images": True,
                    "single_image_review": True,
                    "structured_json": True,
                },
                resource_profile={"capability": "medium-vlm"},
                benchmark={"structured_success_rate": 1.0},
                enabled=True,
            )
        )
    await session.flush()
    source_task = await _succeeded_primary_task(
        session, asset=asset, campaign=campaign, generation=generation
    )
    return program, asset, campaign, source_task, generation


async def _succeeded_primary_task(
    session,
    *,
    asset: Asset,
    campaign: CleanupCampaign,
    generation: AnalysisGeneration,
) -> Task:
    revision_id = asset.current_revision_id
    assert revision_id is not None
    source_task = Task(
        campaign_id=campaign.id,
        connection_id=asset.connection_id,
        asset_id=asset.id,
        asset_revision_id=revision_id,
        analysis_generation_id=generation.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "technical_confidence": 0.8,
            "technical_metrics": {},
            "queue_origin": "live",
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"review-primary-{uuid.uuid4()}",
        state=TaskState.SUCCEEDED,
    )
    session.add(source_task)
    await session.flush()
    primary_node = await session.scalar(
        select(AnalysisNode).where(
            AnalysisNode.generation_id == generation.id,
            AnalysisNode.stage_key == "primary",
        )
    )
    assert primary_node is not None
    primary_node.task_id = source_task.id
    primary_node.state = "succeeded"
    primary_node.required = True
    primary_node.applicable = True
    return source_task


def _star_rating_payload(rating: int = 4, **overrides: object) -> dict[str, object]:
    """Return the bounded star payload the primary review hands to the senior review."""

    return {
        "rating": rating,
        "confidence": 80,
        "sensitivity": "safe",
        "privacy_note": "",
        "people": 2,
        "flags": ["real_interaction"],
        "summary": "two children on a beach",
        "tags": ["beach"],
        **overrides,
    }


async def _primary_proposal(
    session,
    *,
    campaign: CleanupCampaign,
    asset: Asset,
    generation: AnalysisGeneration,
    rating: int,
    **evidence,
) -> Proposal:
    proposal = Proposal(
        campaign_id=campaign.id,
        analysis_generation_id=generation.id,
        proposal_type=ProposalType.RATING,
        target_type="asset",
        target_id=asset.id,
        value={
            **_star_rating_payload(rating),
            "source": "primary_photo_review",
            **evidence,
        },
        confidence=0.9,
        reason_codes=["primary_photo_review"],
        explanation="validated primary result",
        analysis_version=f"test-primary-{uuid.uuid4()}",
        authority_stage="primary",
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def _published_highlight_subject(
    session,
    *,
    primary_rating: int,
    effective_rating: int,
    rating_source: str,
):
    program, asset, campaign, _source_task, generation = await _review_subject(
        session, with_qwen=False
    )
    generation.state = "published"
    generation.published_at = datetime.now(UTC)
    asset.current_generation_id = generation.id
    primary = await _primary_proposal(
        session,
        campaign=campaign,
        asset=asset,
        generation=generation,
        rating=primary_rating,
    )
    session.add(
        AnalysisRun(
            library_program_id=program.id,
            campaign_id=campaign.id,
            analysis_generation_id=generation.id,
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            analysis_type="primary_review",
            processor_version="test-primary",
            model_key="ministral-3:8b-instruct-2512-q4_K_M",
            status="succeeded",
            result_payload=photo_observation_document(rating=primary_rating),
            completed_at=datetime.now(UTC),
        )
    )
    snapshot = await session.get(AssetCurationSnapshot, asset.id)
    if snapshot is None:
        snapshot = AssetCurationSnapshot(asset_id=asset.id)
        session.add(snapshot)
    snapshot.asset_revision_id = asset.current_revision_id
    snapshot.effective_rating = effective_rating
    snapshot.rating_source = rating_source
    snapshot.analysis_complete = True
    await session.flush()
    return asset, campaign, generation, primary, snapshot


async def _qwen_node(session, generation: AnalysisGeneration) -> AnalysisNode:
    node = await session.scalar(
        select(AnalysisNode).where(
            AnalysisNode.generation_id == generation.id,
            AnalysisNode.stage_key == "qwen",
        )
    )
    assert node is not None
    return node


async def _senior_review_task(session, generation: AnalysisGeneration) -> Task | None:
    return await session.scalar(
        select(Task).where(
            Task.analysis_generation_id == generation.id,
            Task.task_type == "asset.deep_review",
        )
    )


def _assert_automatic_senior_review(task: Task, generation: AnalysisGeneration) -> None:
    assert task.task_type == "asset.deep_review"
    assert task.analysis_generation_id == generation.id
    assert task.state == TaskState.QUEUED
    assert task.completion_role == "conditional"
    assert task.routing_key == "medium-vlm"
    assert task.target_ref["queue_origin"] == "live"
    assert task.target_ref["apple_queue_category"] in {"four_star", "five_star", "sensitive"}
    assert task.target_ref["model_key"] == APPLE_ADVISORY_MODEL_KEY
    assert "manual_release_required" not in task.target_ref
    assert "manual_release_approved" not in task.target_ref


@pytest.mark.parametrize(
    ("rating", "sensitivity", "sensitive_content", "expected_reasons"),
    [
        # An explicit class rated 3 or below is enough evidence for blurring;
        # it does not spend the Apple worker model and never lowers the star.
        (1, "intimate_nonsexual", "intimate", None),
        (2, "nudity", "nude", None),
        (3, "erotic", "erotic", None),
        # An undecided class always gets the second local visual reviewer.
        (
            1,
            "uncertain_sensitive",
            "uncertain",
            ["nsfw_privacy_review", "privacy_low_confidence", "privacy_uncertain"],
        ),
        # A strong star escalates as usual; the explicit class rides along so
        # the Advanced review can correct NSFW under-rating.
        (4, "nudity", "nude", ["nsfw_privacy_review", "four_star_candidate"]),
        (5, "erotic", "erotic", ["nsfw_privacy_review", "five_star_highlight"]),
    ],
)
async def test_sensitive_escalation_matrix(
    session,
    rating: int,
    sensitivity: str,
    sensitive_content: str,
    expected_reasons: list[str] | None,
) -> None:
    program, asset, campaign, source_task, generation = await _review_subject(session)
    proposal = await _primary_proposal(
        session,
        campaign=campaign,
        asset=asset,
        generation=generation,
        rating=min(rating, 4),
        sensitivity=sensitivity,
        sensitive_content=sensitive_content,
        privacy_risk=0.85,
        provisional_five_candidate=rating == 5,
        privacy_observation={
            "sensitivity_class": sensitivity,
            "privacy_confidence": 60 if sensitivity == "uncertain_sensitive" else 85,
        },
    )

    queued = await route_primary_review(
        session, asset=asset, source_task=source_task, primary=proposal, program=program
    )

    node = await _qwen_node(session, generation)
    senior = await _senior_review_task(session, generation)
    assert queued is None
    if expected_reasons is None:
        assert senior is None
        assert node.state == "not_applicable"
        assert node.decision["reason_code"] == "not_required_by_routing_policy"
        return
    assert senior is not None
    _assert_automatic_senior_review(senior, generation)
    assert senior.target_ref["escalation_reasons"] == expected_reasons
    assert senior.target_ref["primary_review"]["sensitivity"] == sensitivity
    assert node.task_id == senior.id
    assert node.applicable is True
    assert node.required is False
    assert node.decision["reason_code"] == "senior_review_queued"


async def test_publication_does_not_wait_for_queued_senior_review(session) -> None:
    program, asset, campaign, source_task, generation = await _review_subject(session)
    primary = await _primary_proposal(
        session, campaign=campaign, asset=asset, generation=generation, rating=4
    )
    queued = await route_primary_review(
        session, asset=asset, source_task=source_task, primary=primary, program=program
    )
    assert queued is None
    senior = await _senior_review_task(session, generation)
    assert senior is not None
    assert senior.state == TaskState.QUEUED

    nodes = {
        node.stage_key: node
        for node in await session.scalars(
            select(AnalysisNode).where(AnalysisNode.generation_id == generation.id)
        )
    }
    nodes["embedding"].state = "succeeded"
    nodes["embedding"].output_digest = "embedding"
    session.add(
        AnalysisRun(
            library_program_id=program.id,
            campaign_id=campaign.id,
            analysis_generation_id=generation.id,
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            analysis_type="primary_review",
            processor_version="test-primary",
            model_key="ministral-3:8b-instruct-2512-q4_K_M",
            status="succeeded",
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()

    await advance_analysis_generations(session)

    assert generation.state == "published"
    assert asset.current_generation_id == generation.id
    assert senior.state == TaskState.QUEUED
    assert nodes["qwen"].required is False
    assert nodes["qwen"].applicable is True


async def test_unavailable_senior_model_does_not_hold_generation_open(session) -> None:
    program, asset, campaign, source_task, generation = await _review_subject(
        session, with_qwen=False
    )
    proposal = await _primary_proposal(
        session, campaign=campaign, asset=asset, generation=generation, rating=5
    )

    queued = await route_primary_review(
        session, asset=asset, source_task=source_task, primary=proposal, program=program
    )

    node = await _qwen_node(session, generation)
    assert queued is None
    assert generation.state == "building"
    assert node.state == "not_applicable"
    assert node.required is False
    assert node.decision["reason_code"] == "senior_model_unavailable"
    assert await _senior_review_task(session, generation) is None


async def test_superseded_generation_cancels_stale_senior_review(session) -> None:
    program, asset, campaign, source_task, stale_generation = await _review_subject(session)
    stale_primary = await _primary_proposal(
        session, campaign=campaign, asset=asset, generation=stale_generation, rating=4
    )
    await route_primary_review(
        session, asset=asset, source_task=source_task, primary=stale_primary, program=program
    )
    stale_senior = await _senior_review_task(session, stale_generation)
    assert stale_senior is not None
    assert stale_senior.state == TaskState.QUEUED

    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="owner_rereview",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"kind": "test"},
    )
    source_task = await _succeeded_primary_task(
        session, asset=asset, campaign=campaign, generation=generation
    )
    primary = await _primary_proposal(
        session, campaign=campaign, asset=asset, generation=generation, rating=5
    )

    await route_primary_review(
        session, asset=asset, source_task=source_task, primary=primary, program=program
    )

    senior = await _senior_review_task(session, generation)
    assert senior is not None
    assert senior.id != stale_senior.id
    _assert_automatic_senior_review(senior, generation)
    assert senior.target_ref["escalation_reasons"] == ["five_star_highlight"]
    assert stale_senior.state == TaskState.CANCELLED
    assert stale_senior.last_error_class == "AnalysisGenerationSuperseded"


async def test_advanced_review_five_publishes_as_five_and_is_writeback_eligible(session) -> None:
    from star.analysis.asset_vlm_submission import record_asset_vlm_submission
    from star.domain.schemas import StarRatingResultSubmission
    from star.services.writeback import _eligible_snapshot

    program, asset, campaign, source_task, generation = await _review_subject(session)
    # The primary model answered 5; it is published as a provisional 4.
    primary = await _primary_proposal(
        session,
        campaign=campaign,
        asset=asset,
        generation=generation,
        rating=4,
        model_proposed_rating=5,
        provisional_five_candidate=True,
    )
    await route_primary_review(
        session, asset=asset, source_task=source_task, primary=primary, program=program
    )
    senior = await _senior_review_task(session, generation)
    assert senior is not None
    assert senior.target_ref["escalation_reasons"] == ["five_star_highlight"]
    assert senior.target_ref["apple_queue_category"] == "five_star"
    assert senior.target_ref["primary_five_star_nomination"] is True
    generation.state = "published"
    generation.published_at = datetime.now(UTC)
    asset.current_generation_id = generation.id
    senior.state = TaskState.RUNNING
    await session.flush()

    payload = StarRatingResultSubmission.model_validate(
        {
            **_star_rating_payload(5),
            "sensitivity": "partial_nudity",
            "privacy_note": "visible skin",
            "model_reported_version": APPLE_ADVISORY_MODEL_KEY,
            "runtime": "mlx",
            "duration_ms": 1200,
            "media_provenance": media_provenance(),
        }
    )
    _run, proposal = await record_asset_vlm_submission(session, task=senior, payload=payload)

    assert proposal is not None
    assert proposal.authority_stage == "qwen"
    assert proposal.value["rating"] == 5
    assert proposal.value["model_proposed_rating"] == 5
    assert proposal.value["provisional_five_candidate"] is False
    assert proposal.value["five_star_authority"] is True
    assert proposal.value["five_star_decision"] == "confirmed"
    assert proposal.value["promoted_to_current"] is True
    final = await session.scalar(
        select(Proposal).where(
            Proposal.analysis_generation_id == generation.id,
            Proposal.authority_stage == "final",
        )
    )
    assert final is not None
    assert final.value["rating"] == 5
    assert final.value["authority_source"] == "qwen"
    snapshot = await session.get(AssetCurationSnapshot, asset.id)
    assert snapshot is not None
    assert snapshot.effective_rating == 5
    assert snapshot.rating_source == "promoted_optional_review"
    assert snapshot.evidence_digest
    revision = await session.get(AssetRevision, asset.current_revision_id)
    assert revision is not None
    outcome, rating = _eligible_snapshot(
        asset, revision, snapshot, generation, minimum_confidence=0.5
    )
    assert (outcome, rating) == ("eligible", 5)


async def test_human_anchor_never_enters_automatic_four_to_five_pool(session) -> None:
    (
        anchor_asset,
        anchor_campaign,
        anchor_generation,
        _primary,
        _snapshot,
    ) = await _published_highlight_subject(
        session,
        primary_rating=3,
        effective_rating=5,
        rating_source="star_human",
    )
    session.add(
        HighlightLeagueEntry(
            asset_id=anchor_asset.id,
            analysis_generation_id=anchor_generation.id,
            asset_revision_id=anchor_asset.current_revision_id,
            campaign_id=anchor_campaign.id,
            generation=HIGHLIGHT_GENERATION,
            source_rating=4,
            eligibility_reason=HUMAN_ANCHOR_REASON,
            in_four_star_pass=True,
            five_star_state="human_anchor",
            state="human_anchor",
        )
    )
    automatic_ids: set[uuid.UUID] = set()
    for _index in range(4):
        asset, campaign, generation, primary, _snapshot = await _published_highlight_subject(
            session,
            primary_rating=4,
            effective_rating=4,
            rating_source="model_consensus",
        )
        automatic_ids.add(asset.id)
        session.add(
            HighlightLeagueEntry(
                asset_id=asset.id,
                analysis_generation_id=generation.id,
                asset_revision_id=asset.current_revision_id,
                campaign_id=campaign.id,
                generation=HIGHLIGHT_GENERATION,
                source_rating=4,
                eligibility_reason="completed_four_star_second_pass",
                relative_rating_proposal_id=primary.id,
                in_four_star_pass=True,
                five_star_state="waiting",
                state="four_star_waiting",
            )
        )
    await session.flush()

    selected = await _comparison_candidates(session, selection_phase="four_to_five")

    assert selected is not None
    round_kind, entries = selected
    assert round_kind == "four_to_five"
    assert {entry.asset_id for entry in entries} == automatic_ids
    assert anchor_asset.id not in {entry.asset_id for entry in entries}


async def test_highlight_order_never_changes_a_star(session) -> None:
    (
        _human_asset,
        _campaign,
        _generation,
        _primary,
        human_snapshot,
    ) = await _published_highlight_subject(
        session,
        primary_rating=4,
        effective_rating=5,
        rating_source="immich_human",
    )
    automatic: list[tuple[HighlightLeagueEntry, AssetCurationSnapshot]] = []
    for tier in (4, 5, 4):
        asset, campaign, generation, primary, snapshot = await _published_highlight_subject(
            session,
            primary_rating=4,
            effective_rating=tier,
            rating_source="model_consensus" if tier == 4 else "promoted_optional_review",
        )
        entry = HighlightLeagueEntry(
            asset_id=asset.id,
            analysis_generation_id=generation.id,
            asset_revision_id=asset.current_revision_id,
            campaign_id=campaign.id,
            generation=HIGHLIGHT_GENERATION,
            source_rating=tier,
            eligibility_reason=f"published_{'five' if tier == 5 else 'four'}_star_tier",
            relative_rating_proposal_id=primary.id,
            in_four_star_pass=True,
            four_star_match_count=6,
            sigma=2.0,
            quality_sum=float(60 + 10 * len(automatic)),
            quality_count=1,
            five_star_state="competing",
            state="tier_comparison",
        )
        session.add(entry)
        automatic.append((entry, snapshot))
    await session.flush()

    with patch(
        "star.services.highlight_league.refresh_asset_curation_snapshot",
        new=AsyncMock(return_value=None),
    ):
        assert await _finalize_highlight_order(session) is True
        assert await _finalize_highlight_order(session) is False

    ranks = {entry.asset_id: entry.five_star_rank for entry, _snapshot in automatic}
    five_star_entry = next(entry for entry, _snapshot in automatic if entry.source_rating == 5)
    assert ranks[five_star_entry.asset_id] == 1
    assert sorted(ranks.values()) == [1, 2, 3]
    assert all(entry.state == STATE_ORDERED for entry, _snapshot in automatic)
    assert all(entry.five_star_state == "ordered" for entry, _snapshot in automatic)
    # Ordering is not a rating authority: tiers and snapshots are untouched and
    # no Highlight proposal exists for any ordered photo.
    assert [entry.source_rating for entry, _snapshot in automatic] == [4, 5, 4]
    assert [snapshot.effective_rating for _entry, snapshot in automatic] == [4, 5, 4]
    highlight_proposals = list(
        await session.scalars(select(Proposal).where(Proposal.authority_stage == "highlights"))
    )
    assert highlight_proposals == []
    assert human_snapshot.effective_rating == 5
    assert human_snapshot.rating_source == "immich_human"
