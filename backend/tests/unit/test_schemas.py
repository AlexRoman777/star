from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from star.agent_main import ASSET_VLM_SCHEMA, DEEP_SIGNIFICANCE_SCHEMA
from star.domain.enums import DecisionType
from star.domain.schemas import (
    AssetVlmResultSubmission,
    CampaignCreate,
    DecisionCreate,
    PreferencePhotoAlignmentOutput,
    PreferencePromptCompactionOutput,
    PreferencePromptSynthesisOutput,
    RandomPhotoSampleCreate,
    StarRatingResultSubmission,
    StarRatingV1,
)
from star.domain.schemas.submissions import STAR_FLAGS, StarFlag


def test_zero_is_not_a_persisted_rating() -> None:
    with pytest.raises(ValidationError):
        DecisionCreate(
            expected_revision=1,
            action=DecisionType.RATE,
            value={"rating": 0},
            idempotency_key="rating-test-0001",
        )


def test_campaign_rejects_uncontrolled_automatic_tags() -> None:
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="Unsafe campaign",
            objective="Test the tag allowlist",
            connection_ids=[uuid.uuid4()],
            scope={},
            automatic_tags=["AI/Delete-Candidate"],
        )


def test_campaign_accepts_only_initial_workflow_tags() -> None:
    campaign = CampaignCreate(
        name="Safe campaign",
        objective="Test the tag allowlist",
        connection_ids=[uuid.uuid4()],
        scope={},
        automatic_tags=["AI/Processing-Failed", "AI/Needs-Review"],
    )
    assert campaign.automatic_tags == ["AI/Needs-Review", "AI/Processing-Failed"]


def test_random_photo_sample_accepts_up_to_one_thousand_photos() -> None:
    assert RandomPhotoSampleCreate(size=1_000).size == 1_000

    with pytest.raises(ValidationError):
        RandomPhotoSampleCreate(size=1_001)


def test_vlm_generation_schema_is_the_compact_star_contract() -> None:
    # The model is asked for a star and the little that keeps privacy, series
    # selection, and search working; the wide retained model is never generated.
    assert list(ASSET_VLM_SCHEMA["properties"]) == list(StarRatingV1.model_fields)
    assert set(ASSET_VLM_SCHEMA["required"]) == {
        "rating",
        "confidence",
        "sensitivity",
        "people",
        "summary",
    }
    assert ASSET_VLM_SCHEMA["additionalProperties"] is False
    assert not {"observations", "privacy", "dimensions", "description"}.intersection(
        ASSET_VLM_SCHEMA["properties"]
    )
    assert "title" not in ASSET_VLM_SCHEMA
    assert all("title" not in field for field in ASSET_VLM_SCHEMA["properties"].values())
    properties = ASSET_VLM_SCHEMA["properties"]
    assert properties["rating"]["minimum"] == 1
    assert properties["rating"]["maximum"] == 5
    assert properties["confidence"]["maximum"] == 100
    assert properties["people"]["maximum"] == 100
    assert properties["privacy_note"]["maxLength"] == 120
    assert properties["summary"]["maxLength"] == 120
    assert properties["flags"]["maxItems"] == 8
    assert properties["tags"]["maxItems"] == 5
    assert DEEP_SIGNIFICANCE_SCHEMA != ASSET_VLM_SCHEMA

    # The wide retained submission still validates worker star answers.
    submission_schema = StarRatingResultSubmission.model_json_schema()
    assert {"model_reported_version", "runtime", "duration_ms", "media_provenance"}.issubset(
        submission_schema["required"]
    )
    # The retained wide model keeps its bounds for historical reviews.
    legacy_schema = AssetVlmResultSubmission.model_json_schema()
    assert legacy_schema["properties"]["harsh_critique"]["maxLength"] == 10_000


def test_star_rating_contract_is_lean_and_normalizes_its_lists() -> None:
    assert set(STAR_FLAGS) == set(StarFlag.__args__)
    assert len(STAR_FLAGS) == 24

    star = StarRatingV1.model_validate(
        {
            "rating": 4,
            "confidence": 77,
            "sensitivity": "safe",
            "privacy_note": "must be cleared for a safe photo",
            "people": 2,
            "flags": ["peak_moment", "peak_moment", "closed_eyes"],
            "summary": "Two children running through sprinklers on a lawn.",
            "tags": [" Outdoor ", "outdoor", "Child", "sprinkler", ""],
        }
    )
    assert star.privacy_note == ""
    assert star.flags == ["peak_moment", "closed_eyes"]
    # Tags are a fixed vocabulary: normalised, unique, unknown words dropped.
    assert star.tags == ["outdoor", "child"]

    with pytest.raises(ValidationError):
        StarRatingV1.model_validate(
            {
                "rating": 6,
                "confidence": 50,
                "sensitivity": "safe",
                "people": 0,
                "summary": "x",
            }
        )
    with pytest.raises(ValidationError):
        StarRatingV1.model_validate(
            {
                "rating": 3,
                "confidence": 50,
                "sensitivity": "safe",
                "people": 0,
                "summary": "x",
                "rating_rationale": "prose is not part of the contract",
            }
        )


def test_preference_schemas_publish_per_item_string_bounds() -> None:
    alignment = PreferencePhotoAlignmentOutput.model_json_schema()
    synthesis = PreferencePromptSynthesisOutput.model_json_schema()
    compaction = PreferencePromptCompactionOutput.model_json_schema()

    assert alignment["properties"]["positive_signals"]["items"]["maxLength"] == 180
    assert synthesis["properties"]["learned_preferences"]["items"]["maxLength"] == 220
    assert synthesis["properties"]["model_assessments"]["minItems"] == 3
    assert synthesis["properties"]["model_assessments"]["maxItems"] == 3
    assert compaction["properties"]["proposed_rules"]["items"]["maxLength"] == 260


def test_prompt_synthesis_requires_one_assessment_for_each_production_model_stage() -> None:
    assessment = {
        "stage": "primary_ministral",
        "strengths": ["Keeps the primary review bounded."],
        "weaknesses": ["May miss some owner preferences."],
        "prompt_actions": ["Preserve literal visible evidence."],
        "evidence_limits": ["The evidence is text-only."],
    }
    with pytest.raises(ValidationError, match="each production rating stage once"):
        PreferencePromptSynthesisOutput.model_validate(
            {
                "analysis_report": "A" * 200,
                "current_prompt_assessment": "B" * 40,
                "preference_appendix": "C" * 200,
                "change_summary": "D" * 20,
                "learned_preferences": ["Prefer readable family moments."],
                "evidence_limits": ["Only retained text evidence was assessed."],
                "model_assessments": [assessment, assessment, assessment],
            }
        )


def test_retained_legacy_review_payload_keys_still_validate() -> None:
    from conftest import photo_observation_document
    from star.domain.schemas import PhotoObservationModelOutput

    document = photo_observation_document()
    legacy = dict(document)
    legacy["dossier"] = legacy.pop("observations")

    validated = PhotoObservationModelOutput.model_validate(legacy)
    assert validated.model_dump(mode="json")["observations"] == document["observations"]
