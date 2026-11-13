from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from conftest import media_provenance, photo_observation_document
from star.analysis.absolute_rating import (
    FIVE_STAR_AUTHORITY_STAGE,
    INDIVIDUAL_RATING_CEILING,
    PRIMARY_AUTHORITY_STAGE,
    derive_star_rating_decision,
)
from star.analysis.asset_vlm_submission import (
    _adapt_star_rating,
    _adapt_structured_observation,
    _cull_review_projection,
    _model_preference,
    deep_review_reasons,
    eligible_asset_vlm_models,
    enqueue_primary_review,
)
from star.db.models import Asset, AssetRevision, CleanupCampaign, ModelVersion, Task, User
from star.domain.enums import MediaType, Role
from star.domain.schemas import (
    AssetVlmResultSubmission,
    PhotoObservationResultSubmission,
    StarRatingResultSubmission,
)
from star.domain.schemas.submissions import SensitivityClass
from star.services.analysis_generations import create_analysis_generation


def _routing_model(model_id: str, capability: str) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        version="2026-07",
        runtime="ollama",
        quantization="Q4_K_M",
        license_id="Apache-2.0",
        artifact_checksum="sha256:" + "d" * 64,
        capabilities={
            "images": True,
            "single_image_review": True,
            "structured_json": True,
        },
        resource_profile={"capability": capability},
        benchmark={"structured_success_rate": 1.0},
        enabled=True,
    )


def test_lean_pipeline_prefers_production_nvidia_and_apple_models() -> None:
    small_models = [
        _routing_model("ministral-3:8b-instruct-2512-q4_K_M", "small-vlm"),
        _routing_model("qwen3.5:9b", "small-vlm"),
        _routing_model("gemma4-e4b-vision:q4_0", "small-vlm"),
    ]
    deep_models = [
        _routing_model("gemma-3-27b-it-qat-mlx", "medium-vlm"),
        _routing_model("qwen/qwen3.6-27b", "medium-vlm"),
    ]

    assert min(small_models, key=_model_preference).model_id == (
        "ministral-3:8b-instruct-2512-q4_K_M"
    )
    assert min(deep_models, key=_model_preference).model_id == "qwen/qwen3.6-27b"


async def test_model_selection_can_pin_the_senior_runtime(session) -> None:
    ollama = _routing_model("qwen3.8:27b", "medium-vlm")
    mlx = _routing_model("qwen3.8:27b", "medium-vlm")
    mlx.runtime = "mlx"
    mlx.version = "mlx-8bit"
    session.add_all([ollama, mlx])
    await session.flush()

    selected = await eligible_asset_vlm_models(
        session, model_keys=["qwen3.8:27b"], runtimes=("mlx",), limit=1
    )

    assert selected == [mlx]


@pytest.mark.parametrize(
    ("rating", "expected_reason"),
    [(4, "four_star_candidate"), (5, "five_star_highlight")],
)
def test_every_nvidia_four_or_five_star_dossier_requests_deep_review(
    rating: int,
    expected_reason: str,
) -> None:
    payload = AssetVlmResultSubmission.model_construct(
        proposed_rating=rating,
        uncertainty=0.05,
        sensitive_content="no",
        privacy_risk=0.0,
        recoverability="none",
        recovery_confidence=1.0,
        preserve_despite_technical_faults=False,
        visible_people_count=0,
        has_children=False,
        family_value=0.0,
        documentary_value=0.0,
    )

    assert expected_reason in deep_review_reasons(payload, technical_rating=rating)


def test_asset_vlm_description_has_no_artificial_character_limit() -> None:
    schema = AssetVlmResultSubmission.model_json_schema()["properties"]["description"]

    assert schema["minLength"] == 1
    assert "maxLength" not in schema


def test_one_primary_star_is_only_an_owner_cull_review_candidate() -> None:
    payload = AssetVlmResultSubmission.model_construct(
        proposed_rating=1,
        deletion_review_candidate=True,
        is_actually_trash=True,
    )

    projection = _cull_review_projection(payload)

    assert projection == {
        "cull_review_candidate": True,
        "cull_review_authority": "owner_only",
        "deletion_review_candidate": False,
        "is_actually_trash": False,
        "legacy_model_cull_signal": True,
    }


def test_structured_primary_one_star_never_sets_legacy_deletion_flag() -> None:
    payload = PhotoObservationResultSubmission.model_validate(
        {
            **photo_observation_document(rating=1),
            "model_reported_version": "ministral-test",
            "runtime": "ollama",
            "duration_ms": 10,
            "media_provenance": media_provenance(),
        }
    )

    adapted, decision = _adapt_structured_observation(payload, primary_semantic_score=None)

    assert decision.rating == 1
    assert adapted.proposed_rating == 1
    assert adapted.deletion_review_candidate is False


def _star_payload(rating: int, **overrides: object) -> StarRatingResultSubmission:
    document: dict[str, object] = {
        "rating": rating,
        "confidence": 80,
        "sensitivity": "safe",
        "privacy_note": "",
        "people": 2,
        "flags": ["peak_moment", "vivid_color_or_light"],
        "summary": "Two children laughing on a beach at sunset.",
        "tags": ["beach", "child"],
        "model_reported_version": "ministral-test",
        "runtime": "ollama",
        "duration_ms": 10,
        "media_provenance": media_provenance(),
    }
    document.update(overrides)
    return StarRatingResultSubmission.model_validate(document)


def test_primary_model_five_publishes_as_five_and_asks_for_the_advanced_check() -> None:
    adapted, decision = _adapt_star_rating(_star_payload(5), technical_metrics=None)

    assert INDIVIDUAL_RATING_CEILING == 5
    assert decision.model_proposed_rating == 5
    assert decision.rating == 5
    assert decision.provisional_five_candidate is True
    assert adapted.proposed_rating == 5
    assert adapted.why_not_five == ""
    assert adapted.why_five.startswith("Strong execution")


def test_advanced_review_publishes_a_model_five_as_five() -> None:
    adapted, decision = _adapt_star_rating(
        _star_payload(5), technical_metrics=None, authority_stage=FIVE_STAR_AUTHORITY_STAGE
    )

    assert decision.model_proposed_rating == 5
    assert decision.rating == 5
    assert decision.provisional_five_candidate is False
    assert decision.tier == "individual_5_star"
    assert adapted.proposed_rating == 5
    assert adapted.why_not_five == ""
    assert adapted.why_five.startswith("Strong execution and family or documentary value")
    assert "peak moment" in adapted.why_five.lower()


def test_no_technical_cap_lowers_a_model_five_any_more() -> None:
    """v209 retired the technical preview; no post-inference cap touches a star."""

    _adapted, decision = _adapt_star_rating(
        _star_payload(5),
        technical_metrics={"total_blur_risk": 0.99, "sharpness": 0.1},
        authority_stage=FIVE_STAR_AUTHORITY_STAGE,
    )

    assert decision.rating == 5
    assert decision.hard_cap is None
    assert decision.cap_reasons == ()
    assert decision.provisional_five_candidate is False


@pytest.mark.parametrize("sensitivity", list(SensitivityClass.__args__))
@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("authority_stage", [PRIMARY_AUTHORITY_STAGE, FIVE_STAR_AUTHORITY_STAGE])
def test_sensitivity_never_changes_the_published_star(
    sensitivity: str, rating: int, authority_stage: str
) -> None:
    """NSFW is a privacy category, not a rating: no class moves a star by code."""

    safe, safe_decision = _adapt_star_rating(
        _star_payload(rating, sensitivity="safe"),
        technical_metrics=None,
        authority_stage=authority_stage,
    )
    adapted, decision = _adapt_star_rating(
        _star_payload(rating, sensitivity=sensitivity, privacy_note="model flagged it"),
        technical_metrics=None,
        authority_stage=authority_stage,
    )
    bare = derive_star_rating_decision(
        rating=rating, confidence=80, authority_stage=authority_stage
    )

    expected = rating  # every stage publishes the star it believes in
    assert decision.rating == bare.rating == safe_decision.rating == expected
    assert decision.model_proposed_rating == rating
    assert adapted.proposed_rating == safe.proposed_rating == expected
    assert decision.provisional_five_candidate == safe_decision.provisional_five_candidate
    assert adapted.privacy_risk == (0.0 if sensitivity == "safe" else 0.85)


def test_sub_five_star_keeps_the_flaw_based_reason() -> None:
    flawed, _ = _adapt_star_rating(_star_payload(4, flags=["closed_eyes"]), technical_metrics=None)
    clean, _ = _adapt_star_rating(_star_payload(3, flags=[]), technical_metrics=None)

    assert flawed.why_not_five.startswith("Marked down for")
    assert flawed.why_five == ""
    assert clean.why_not_five == (
        "Does not reach the exceptional threshold required for five stars."
    )


def test_legacy_cull_flags_cannot_nominate_a_non_one_star_photo() -> None:
    payload = AssetVlmResultSubmission.model_construct(
        proposed_rating=2,
        deletion_review_candidate=True,
        is_actually_trash=True,
    )

    assert _cull_review_projection(payload)["cull_review_candidate"] is False


async def test_manual_request_identity_reuses_generation_primary_task(session) -> None:
    owner = User(
        username="generation-task-owner",
        display_name="Generation Task Owner",
        role=Role.ADMINISTRATOR,
    )
    session.add(owner)
    await session.flush()
    campaign = CleanupCampaign(
        name="Generation task identity",
        objective="Verify one Primary per immutable generation",
        scope={},
        created_by=owner.id,
    )
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="generation-task-identity",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    model = _routing_model("ministral-3:8b-instruct-2512-q4_K_M", "small-vlm")
    session.add_all([campaign, asset, model])
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest="generation-task-identity-revision",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=owner.id,
        reason="manual_reanalysis",
        mode="complete_review",
        start_stage="technical",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"kind": "test"},
    )

    first = await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=campaign.id,
        model=model,
        manual_request_id=uuid.uuid4(),
        queue_origin="user",
        analysis_generation_id=generation.id,
    )
    second = await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=campaign.id,
        model=model,
        manual_request_id=uuid.uuid4(),
        queue_origin="user",
        analysis_generation_id=generation.id,
    )

    assert second.id == first.id
    assert (
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.analysis_generation_id == generation.id,
                Task.task_type == "asset.primary_review",
            )
        )
        == 1
    )
