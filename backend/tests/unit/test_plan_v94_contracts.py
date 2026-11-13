from __future__ import annotations

from pathlib import Path

from conftest import media_provenance
from star.domain.schemas import (
    PhotoObservationResultSubmission,
    PhotoObservationsV1,
    StarRatingV1,
)

ROOT = Path(__file__).resolve().parents[3]


def _observation(**dossier_changes: object) -> PhotoObservationResultSubmission:
    review: dict[str, object] = {
        "orientation_status": "normal",
        "content_path": "human_moment",
        "frame_function": "deliberate",
        "primary_subject_type": "people",
        "primary_subject_location": "center",
        "primary_subject_complete": "yes",
        "secondary_subjects_complete": "yes",
        "important_subject_count": 2,
        "visible_people_count": 2,
        "intended_moment_readable": "yes",
        "faces_expected": "yes",
        "readable_face_count": 2,
        "face_visibility": "clear",
        "gaze_visibility": "clear",
        "expression_visibility": "clear",
        "eye_state": "open",
        "camera_engagement": "direct",
        "subject_interaction": "excellent",
        "pose_readiness": "ready",
        "timing_quality": "excellent",
        "dominant_body_fragment": "none",
        "important_face_or_head_cutoff": "none",
        "subject_overlap": "none",
        "subject_obstruction": "none",
        "subject_focus": "excellent",
        "face_focus": "excellent",
        "motion_blur_impact": "none",
        "subject_exposure": "excellent",
        "tonal_loss_impact": "none",
        "noise_artifact_impact": "none",
        "composition_hierarchy": "excellent",
        "framing_quality": "excellent",
        "edge_intrusion": "none",
        "spacing_balance": "excellent",
        "background_distraction": "none",
        "subject_background_separation": "excellent",
        "light_quality": "excellent",
        "color_quality": "excellent",
        "atmosphere_strength": "excellent",
        "visual_interest": "excellent",
        "meaningful_action_or_event": "strong",
        "documentary_value": 95,
        "visible_archive_usefulness": 95,
        "practical_use": "print",
        "editing_recoverability": "high",
        "alternate_subject_strength": "excellent",
        "strongest_visible_strength": "clear visible moment",
        "strongest_visible_flaw": "none material",
        "hard_disqualifiers": [],
        "observation_confidence": 95,
    }
    review.update(dossier_changes)
    return PhotoObservationResultSubmission.model_validate(
        {
            "observations": review,
            "privacy": {
                "sensitivity_class": "safe",
                "visible_sensitive_evidence": "",
                "nudity_level": "none",
                "sexual_or_erotic_context": "no",
                "intimate_nonsexual_context": "no",
                "privacy_confidence": 95,
                "privacy_route": "normal",
            },
            "dimensions": {
                "purpose_and_intentional_success": 95,
                "subject_success": 95,
                "timing_or_visible_peak": 95,
                "composition_and_framing": 95,
                "emotional_or_atmospheric_impact": 95,
                "archive_and_documentary_usefulness": 95,
                "uniqueness": 95,
            },
            "rating": 5,
            "rating_confidence": 95,
            "rating_rationale": "A rare visible highlight with exceptional execution.",
            "hard_critique": "No material visible flaw limits the photograph.",
            "why_not_five": "",
            "sensitivity": "safe",
            "uncertainties": [],
            "description": "Two visible people in a deliberate photograph.",
            "content_tags": ["people"],
            "model_reported_version": "test",
            "runtime": "test",
            "duration_ms": 1,
            "media_provenance": media_provenance(),
        }
    )


def test_primary_review_asks_for_the_star_contract_only() -> None:
    assert list(StarRatingV1.model_fields) == [
        "rating",
        "confidence",
        "sensitivity",
        "privacy_note",
        "people",
        "flags",
        "summary",
        "tags",
    ]
    assert {name for name, field in StarRatingV1.model_fields.items() if field.is_required()} == {
        "rating",
        "confidence",
        "sensitivity",
        "people",
        "summary",
    }
    # The wide dossier is retained data: only the answers that still carry
    # rating logic remain required, everything else defaults.
    assert len(PhotoObservationsV1.model_fields) == 50
    required = {
        name for name, field in PhotoObservationsV1.model_fields.items() if field.is_required()
    }
    assert len(required) == 18
    assert {"frame_function", "eye_state", "pose_readiness", "hard_disqualifiers"}.issubset(
        required
    )
    assert not required.intersection(StarRatingV1.model_fields)


def test_individual_review_is_one_visual_pass_without_reconciliation() -> None:
    agent_dir = ROOT / "backend/src/star/agent"
    source = "\n".join(path.read_text(encoding="utf-8") for path in agent_dir.glob("*.py"))
    assert "photo_deep_reconciliation" not in source
    assert "photo_deep_visual" not in source
    assert "photo_deep_evidence_audit" not in source
    assert "photo_deep_final_verdict" not in source
    assert 'inference_task = f"photo_deep_review:{prompt_version}" if is_senior' in source
    # The released default effort is off for both individual reviews (round 3);
    # the owner's manifest settings may move the Advanced review to low/medium
    # (test_agent_benchmark pins that the default keeps the request
    # byte-identical).
    assert 'stage_settings, "off"' in source
    assert '"medium" if is_senior else "off"' not in source
    assert 'visual_context["primary_review"] = safe_primary_review' in source


def test_media_free_audit_surface_is_complete() -> None:
    source = (ROOT / "backend/src/star/api/routers/audit_access.py").read_text(encoding="utf-8")
    for route in ("/inference-profiles",):
        assert f'"{route}"' in source
