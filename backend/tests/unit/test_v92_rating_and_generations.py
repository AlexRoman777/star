from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from conftest import media_provenance
from star.analysis.absolute_rating import derive_absolute_rating
from star.db.models import AnalysisNode, Asset, AssetRevision, Task
from star.domain.enums import MediaType, TaskState
from star.domain.schemas import (
    PhotoObservationResultSubmission,
    PhotoObservationsV1,
    StarRatingV1,
)
from star.services.analysis_generations import (
    create_analysis_generation,
    publish_generation,
)


def _observation(
    *,
    frame_function: str = "deliberate",
    eye_state: str = "open",
    pose_readiness: str = "ready",
    secondary_subjects_complete: str = "yes",
    important_subject_count: int = 4,
    meaningful_action_or_event: str = "strong",
    alternate_subject_strength: str = "excellent",
    dimension_score: int = 95,
    rating: int = 5,
) -> PhotoObservationResultSubmission:
    quality = "excellent"
    review = {
        "orientation_status": "normal",
        "content_path": "human_moment",
        "frame_function": frame_function,
        "primary_subject_type": "people",
        "primary_subject_location": "center",
        "primary_subject_complete": "yes",
        "secondary_subjects_complete": secondary_subjects_complete,
        "important_subject_count": important_subject_count,
        "visible_people_count": 4,
        "intended_moment_readable": "yes",
        "faces_expected": "yes",
        "readable_face_count": 4,
        "face_visibility": "clear",
        "gaze_visibility": "clear",
        "expression_visibility": "clear",
        "eye_state": eye_state,
        "camera_engagement": "direct",
        "subject_interaction": quality,
        "pose_readiness": pose_readiness,
        "timing_quality": quality,
        "dominant_body_fragment": "none",
        "important_face_or_head_cutoff": "none",
        "subject_overlap": "none",
        "subject_obstruction": "none",
        "subject_focus": quality,
        "face_focus": quality,
        "motion_blur_impact": "none",
        "subject_exposure": quality,
        "tonal_loss_impact": "none",
        "noise_artifact_impact": "none",
        "composition_hierarchy": quality,
        "framing_quality": quality,
        "edge_intrusion": "none",
        "spacing_balance": quality,
        "background_distraction": "none",
        "subject_background_separation": quality,
        "light_quality": quality,
        "color_quality": quality,
        "atmosphere_strength": quality,
        "visual_interest": quality,
        "meaningful_action_or_event": meaningful_action_or_event,
        "documentary_value": dimension_score,
        "visible_archive_usefulness": dimension_score,
        "practical_use": "print",
        "editing_recoverability": "high",
        "alternate_subject_strength": alternate_subject_strength,
        "strongest_visible_strength": "Complete readable family moment",
        "strongest_visible_flaw": "No material visible flaw",
        "hard_disqualifiers": [],
        "observation_confidence": 95,
    }
    dimensions = {
        "purpose_and_intentional_success": dimension_score,
        "subject_success": dimension_score,
        "timing_or_visible_peak": dimension_score,
        "composition_and_framing": dimension_score,
        "emotional_or_atmospheric_impact": dimension_score,
        "archive_and_documentary_usefulness": dimension_score,
        "uniqueness": dimension_score,
    }
    return PhotoObservationResultSubmission.model_validate(
        {
            "observations": review,
            "privacy": {
                "sensitivity_class": "safe",
                "visible_sensitive_evidence": "",
                "nudity_level": "none",
                "sexual_or_erotic_context": "no",
                "intimate_nonsexual_context": "no",
                "privacy_confidence": 98,
                "privacy_route": "normal",
            },
            "dimensions": dimensions,
            "rating": rating,
            "rating_confidence": 95,
            "rating_rationale": "The visible evidence supports the individual rating.",
            "hard_critique": "The strongest visible limitation is stated directly.",
            "why_not_five": "" if rating == 5 else "It lacks rare visible impact.",
            "sensitivity": "safe",
            "uncertainties": [],
            "description": "Four people deliberately posing with readable faces.",
            "content_tags": ["family", "portrait"],
            "model_reported_version": "test-model",
            "runtime": "test",
            "duration_ms": 1,
            "media_provenance": media_provenance(),
        }
    )


def test_star_contract_is_the_only_generated_review_shape() -> None:
    # The model is asked for eight fields; the wide observation dossier is
    # retained data whose remaining required answers still carry rating logic.
    schema = StarRatingV1.model_json_schema()

    assert list(schema["properties"]) == [
        "rating",
        "confidence",
        "sensitivity",
        "privacy_note",
        "people",
        "flags",
        "summary",
        "tags",
    ]
    assert set(schema["required"]) == {"rating", "confidence", "sensitivity", "people", "summary"}

    observations = PhotoObservationsV1.model_json_schema()
    assert len(observations["properties"]) == 50
    assert set(observations["required"]) == {
        "documentary_value",
        "editing_recoverability",
        "eye_state",
        "face_focus",
        "frame_function",
        "hard_disqualifiers",
        "important_face_or_head_cutoff",
        "motion_blur_impact",
        "pose_readiness",
        "strongest_visible_flaw",
        "strongest_visible_strength",
        "subject_exposure",
        "subject_focus",
        "subject_obstruction",
        "subject_overlap",
        "timing_quality",
        "visible_archive_usefulness",
        "visible_people_count",
    }


def test_individual_five_publishes_as_five_and_asks_for_the_advanced_check() -> None:
    decision = derive_absolute_rating(
        _observation(),
        technical_score=95,
        apple_score=90,
    )

    assert decision.model_proposed_rating == 5
    assert decision.rating == 5
    assert decision.tier == "individual_5_star"
    assert decision.provisional_five_candidate is True

    advanced = derive_absolute_rating(
        _observation(), technical_score=None, apple_score=None, authority_stage="qwen"
    )
    assert advanced.rating == 5
    assert advanced.provisional_five_candidate is False


async def test_generation_publication_is_atomic_and_new_revision_supersedes_building_work(
    session,
) -> None:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="generation-asset",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest="generation-revision",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id

    first = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="initial",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={},
    )
    first_nodes = list(
        await session.scalars(select(AnalysisNode).where(AnalysisNode.generation_id == first.id))
    )
    for node in first_nodes:
        node.state = "succeeded"
    assert await publish_generation(session, first.id) is True
    assert asset.current_generation_id == first.id

    second = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="replacement",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={},
    )
    queued = Task(
        task_type="asset.primary_review",
        target_ref={"asset_id": str(asset.id)},
        requirements={},
        idempotency_key="generation-obsolete-task",
        state=TaskState.QUEUED,
        analysis_generation_id=second.id,
    )
    session.add(queued)
    await session.flush()
    assert asset.current_generation_id == first.id

    replacement_revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest="replacement-generation-revision",
        normalized_metadata={"updated": True},
    )
    session.add(replacement_revision)
    await session.flush()
    asset.current_revision_id = replacement_revision.id

    third = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="newest",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={},
    )
    assert second.state == "superseded"
    assert queued.state == TaskState.CANCELLED
    assert asset.current_generation_id == first.id

    third_nodes = list(
        await session.scalars(select(AnalysisNode).where(AnalysisNode.generation_id == third.id))
    )
    for node in third_nodes:
        node.state = "succeeded"
    assert await publish_generation(session, third.id) is True
    assert asset.current_generation_id == third.id
    assert first.state == "superseded"


async def test_diagnostic_generation_never_supersedes_required_review(session) -> None:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="diagnostic-generation-asset",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest="diagnostic-generation-revision",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id

    required = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="automatic_review",
        mode="complete_review",
        start_stage="source",
        reuse_policy="compatible",
        publication_policy="atomic",
        scope={"kind": "automatic"},
    )
    diagnostic = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="owner_technical_check",
        mode="step_only",
        start_stage="source",
        reuse_policy="none",
        publication_policy="diagnostic",
        scope={"kind": "manual"},
        supersede_active=False,
    )

    assert required.state == "building"
    assert diagnostic.state == "building"
    assert diagnostic.sequence == required.sequence + 1
