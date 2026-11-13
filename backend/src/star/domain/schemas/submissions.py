from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from star.domain.schemas._base import ApiSchema

PreferenceInsight180 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=180),
]
PreferenceRule220 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=220),
]
PreferenceCompactionItem260 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=260),
]


class PreferenceModelAssessment(ApiSchema):
    """One role-specific finding retained with a prompt-adviser proposal."""

    model_config = ConfigDict(extra="forbid")

    stage: Literal["primary_ministral", "quick_qwen", "highlight_qwen"]
    strengths: list[PreferenceCompactionItem260] = Field(min_length=1, max_length=8)
    weaknesses: list[PreferenceCompactionItem260] = Field(min_length=1, max_length=8)
    prompt_actions: list[PreferenceCompactionItem260] = Field(min_length=1, max_length=8)
    evidence_limits: list[PreferenceCompactionItem260] = Field(min_length=1, max_length=6)


class MediaProcessingProvenance(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    mode: Literal[
        "source_derivative",
        "exif_oriented_jpeg",
        "bounded_webp",
        "labelled_lossless_webp",
        "bounded_jpeg_q80",
        "labelled_jpeg_q80",
        "labelled_contact_sheet_jpeg_q80",
    ]
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(ge=16, le=100_000)
    height: int = Field(ge=16, le=100_000)
    max_edge: int | None = Field(default=None, ge=128, le=2048)
    encoding_quality: int | None = Field(default=None, ge=1, le=100)
    orientation_policy: Literal["exif_transpose", "provider_native"]
    cropped: bool
    upscaled: bool


class MediaProvenance(ApiSchema):
    """Persisted source and normalization facts for one leased media input."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["immich-720p-thumbnail__4k-preview__full-dimension-webp-v1"]
    # Quick Qwen groups intentionally request Immich's native 720-pixel
    # thumbnail. Individual reviews and Highlights request the full-size route,
    # which may still deliver a recorded preview fallback.
    requested_variant: Literal["thumbnail", "fullsize"]
    delivered_variant: Literal["thumbnail", "preview", "fullsize"]
    delivered_role: Literal["720p_thumbnail", "4k_preview", "full_dimension_webp"]
    source_media_type: Literal["image/jpeg", "image/png", "image/webp"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_width: int = Field(ge=16, le=100_000)
    source_height: int = Field(ge=16, le=100_000)
    visual_revision: str = Field(min_length=1, max_length=256)
    fallback_reason: str | None = Field(default=None, min_length=1, max_length=128)
    contract_conformant: bool
    asset_reference: str = Field(min_length=1, max_length=128)
    processing: MediaProcessingProvenance


class VisualFingerprintSubmission(ApiSchema):
    """Deterministic layout fingerprints that drive burst and series grouping.

    Computed by the similarity-embedding worker since v209 round 3 (they used
    to arrive with the retired Technical preview). Never a rating vote.
    """

    model_config = ConfigDict(extra="forbid")

    perceptual_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    perceptual_hash_256: str = Field(pattern=r"^[0-9a-f]{64}$")
    difference_hash_256: str = Field(pattern=r"^[0-9a-f]{64}$")
    color_signature: str = Field(pattern=r"^[0-9a-f]{96}$")
    ssim_luma_signature: str = Field(pattern=r"^[0-9a-f]{512}$")


class SemanticEmbeddingResultSubmission(ApiSchema):
    processor_version: Literal["dinov2-small-384-v1"]
    model_key: Literal["facebook/dinov2-small"]
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding: list[float] = Field(min_length=384, max_length=384)
    duration_ms: int = Field(ge=0, le=3_600_000)
    media_provenance: MediaProvenance
    # Optional so a worker from before v209 round 3 still validates; without
    # it the photo keeps whatever fingerprints an earlier stage recorded.
    fingerprints: VisualFingerprintSubmission | None = None

    @field_validator("embedding")
    @classmethod
    def validate_embedding(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("semantic embedding must contain finite values")
        norm = math.sqrt(sum(item * item for item in value))
        if not 0.999 <= norm <= 1.001:
            raise ValueError("semantic embedding must be L2-normalized")
        return value


class EyeStateFaceSubmission(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    left_eye_openness: float = Field(ge=0, le=1)
    right_eye_openness: float = Field(ge=0, le=1)


class SubjectDetectionSubmission(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    subject_class: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)


class GroupVlmRank(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(ge=1, le=8)
    score: float = Field(ge=0, le=1)
    proposed_rating: int = Field(ge=1, le=5)
    strengths: list[str] = Field(default_factory=list, max_length=8)
    weaknesses: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("strengths", "weaknesses")
    @classmethod
    def validate_observations(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("VLM observations must contain 1 to 256 characters")
        return value


class HighlightComparisonRank(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(ge=1, le=8)
    quality: int = Field(ge=0, le=100)
    provisional_rating: int | None = Field(default=None, ge=1, le=5)
    summary: str | None = Field(default=None, min_length=1, max_length=160)
    confidence: int | None = Field(default=None, ge=0, le=100)


class PreferencePhotoAlignmentOutput(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    assessment: Literal[
        "supports_owner",
        "partly_supports_owner",
        "personal_preference",
        "insufficient_visible_evidence",
    ]
    rating_gap_summary: str = Field(min_length=10, max_length=320)
    positive_signals: list[PreferenceInsight180] = Field(default_factory=list, max_length=8)
    negative_signals: list[PreferenceInsight180] = Field(default_factory=list, max_length=8)
    rule_candidates: list[PreferenceInsight180] = Field(default_factory=list, max_length=6)
    confidence: int = Field(ge=0, le=100)

    @field_validator("positive_signals", "negative_signals", "rule_candidates")
    @classmethod
    def validate_bounded_insights(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 180 for value in values):
            raise ValueError("preference insights must contain 1 to 180 characters")
        return values


class PreferencePromptSynthesisOutput(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    analysis_report: str = Field(min_length=200, max_length=12_000)
    current_prompt_assessment: str = Field(min_length=40, max_length=2_000)
    preference_appendix: str = Field(min_length=200, max_length=6_000)
    change_summary: str = Field(min_length=20, max_length=400)
    learned_preferences: list[PreferenceRule220] = Field(min_length=1, max_length=12)
    evidence_limits: list[PreferenceRule220] = Field(min_length=1, max_length=8)
    model_assessments: list[PreferenceModelAssessment] = Field(min_length=3, max_length=3)

    @field_validator("learned_preferences", "evidence_limits")
    @classmethod
    def validate_bounded_rules(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 220 for value in values):
            raise ValueError("preference rules must contain 1 to 220 characters")
        return values

    @model_validator(mode="after")
    def validate_complete_model_assessments(self) -> PreferencePromptSynthesisOutput:
        expected = {"primary_ministral", "quick_qwen", "highlight_qwen"}
        actual = {assessment.stage for assessment in self.model_assessments}
        if actual != expected:
            raise ValueError("model assessments must cover each production rating stage once")
        return self


class PreferencePromptCompactionOutput(ApiSchema):
    """One text-only map step over a complete slice of owner preference evidence."""

    model_config = ConfigDict(extra="forbid")

    chunk_summary: str = Field(min_length=120, max_length=6_000)
    recurring_preferences: list[PreferenceCompactionItem260] = Field(
        default_factory=list,
        max_length=16,
    )
    contradictions: list[PreferenceCompactionItem260] = Field(
        default_factory=list,
        max_length=12,
    )
    proposed_rules: list[PreferenceCompactionItem260] = Field(
        default_factory=list,
        max_length=16,
    )
    evidence_limits: list[PreferenceCompactionItem260] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator(
        "recurring_preferences",
        "contradictions",
        "proposed_rules",
        "evidence_limits",
    )
    @classmethod
    def validate_bounded_compaction(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 260 for value in values):
            raise ValueError("preference compaction items must contain 1 to 260 characters")
        return values


class InferenceInvocationSubmission(ApiSchema):
    """Sanitized accounting for one provider-level inference invocation."""

    model_config = ConfigDict(extra="forbid")
    started_at: datetime | None = None
    completed_at: datetime | None = None
    timings_ms: dict[
        Literal["load", "prompt_eval", "generation"], Annotated[int, Field(ge=0, le=3_600_000)]
    ] = Field(default_factory=dict, max_length=3)

    phase: str = Field(min_length=1, max_length=64)
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=128)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_count: int = Field(ge=0, le=8)
    request_count: int = Field(default=1, ge=1, le=4)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0, le=3_600_000)


class PreferencePhotoAlignmentSubmission(PreferencePhotoAlignmentOutput):
    model_reported_version: str = Field(min_length=1, max_length=256)
    runtime: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(ge=0, le=3_600_000)
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)


class PreferencePromptSynthesisSubmission(PreferencePromptSynthesisOutput):
    model_reported_version: str = Field(min_length=1, max_length=256)
    runtime: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(ge=0, le=3_600_000)
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)


class PreferencePromptCompactionSubmission(PreferencePromptCompactionOutput):
    model_reported_version: str = Field(min_length=1, max_length=256)
    runtime: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(ge=0, le=3_600_000)
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)


class HighlightComparisonResultSubmission(ApiSchema):
    """Bounded group ranking; quick cohorts may also carry provisional stars."""

    model_config = ConfigDict(extra="forbid")

    ranking: list[HighlightComparisonRank] = Field(min_length=2, max_length=8)
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0, le=3_600_000)
    media_provenance: list[MediaProvenance] = Field(min_length=2, max_length=8)
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_comparison(self) -> HighlightComparisonResultSubmission:
        indexes = [item.candidate_index for item in self.ranking]
        if len(indexes) != len(set(indexes)):
            raise ValueError("candidate indexes must be unique")
        if len(self.media_provenance) != len(self.ranking):
            raise ValueError("media provenance must cover every comparison candidate")
        references = [item.asset_reference for item in self.media_provenance]
        if len(references) != len(set(references)):
            raise ValueError("comparison media references must be unique")
        return self


ObservationQuality = Literal[
    "excellent", "good", "ordinary", "weak", "failed", "unknown", "not_applicable"
]


ObservationSeverity = Literal["none", "minor", "material", "severe", "unknown", "not_applicable"]


ObservationVisibility = Literal[
    "clear", "partial", "unreadable", "hidden", "unknown", "not_applicable"
]


class PhotoObservationsV1(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    orientation_status: Literal["normal", "rotated", "incorrect", "unknown"] = "unknown"
    content_path: Literal[
        "human_moment",
        "landscape_or_travel",
        "pet_or_animal",
        "flowers_or_still_life",
        "abstract_or_fine_art",
        "everyday_documentary",
        "utility_or_reference",
        "unknown",
    ] = "unknown"
    frame_function: Literal[
        "deliberate",
        "documentary",
        "accidental",
        "test",
        "setup",
        "transitional",
        "repositioning",
        "unknown",
    ]
    primary_subject_type: Literal[
        "person", "people", "animal", "landscape", "object", "abstract", "document", "unknown"
    ] = "unknown"
    primary_subject_location: Literal[
        "center", "left", "right", "upper", "lower", "distributed", "unknown"
    ] = "unknown"
    primary_subject_complete: Literal["yes", "no", "unknown", "not_applicable"] = "unknown"
    secondary_subjects_complete: Literal["yes", "no", "unknown", "not_applicable"] = (
        "not_applicable"
    )
    important_subject_count: int = Field(default=0, ge=0, le=100)
    visible_people_count: int = Field(ge=0, le=100)
    intended_moment_readable: Literal["yes", "no", "unknown"] = "unknown"

    faces_expected: Literal["yes", "no", "unknown", "not_applicable"] = "unknown"
    readable_face_count: int = Field(default=0, ge=0, le=100)
    face_visibility: ObservationVisibility = "unknown"
    gaze_visibility: ObservationVisibility = "unknown"
    expression_visibility: ObservationVisibility = "unknown"
    eye_state: Literal[
        "open",
        "accidental_closed",
        "intentional_closed",
        "mixed",
        "unknown",
        "not_applicable",
    ]
    camera_engagement: Literal[
        "direct", "interaction", "candid_action", "none", "unknown", "not_applicable"
    ] = "unknown"
    subject_interaction: ObservationQuality = "unknown"
    pose_readiness: Literal["ready", "not_ready", "transitional", "unknown", "not_applicable"]
    timing_quality: ObservationQuality

    dominant_body_fragment: ObservationSeverity = "unknown"
    important_face_or_head_cutoff: ObservationSeverity
    subject_overlap: ObservationSeverity
    subject_obstruction: ObservationSeverity
    subject_focus: ObservationQuality
    face_focus: ObservationQuality
    motion_blur_impact: ObservationSeverity
    subject_exposure: ObservationQuality
    tonal_loss_impact: ObservationSeverity = "unknown"
    noise_artifact_impact: ObservationSeverity = "unknown"

    composition_hierarchy: ObservationQuality = "unknown"
    framing_quality: ObservationQuality = "unknown"
    edge_intrusion: ObservationSeverity = "unknown"
    spacing_balance: ObservationQuality = "unknown"
    background_distraction: ObservationSeverity = "unknown"
    subject_background_separation: ObservationQuality = "unknown"
    light_quality: ObservationQuality = "unknown"
    color_quality: ObservationQuality = "unknown"
    atmosphere_strength: ObservationQuality = "unknown"
    visual_interest: ObservationQuality = "unknown"

    meaningful_action_or_event: Literal["strong", "present", "weak", "none", "unknown"] = "unknown"
    documentary_value: int = Field(ge=0, le=100)
    visible_archive_usefulness: int = Field(ge=0, le=100)
    practical_use: Literal[
        "print", "display", "share", "archive", "reference", "none", "unknown"
    ] = "unknown"
    editing_recoverability: Literal["none", "low", "moderate", "high", "unknown"]
    alternate_subject_strength: ObservationQuality = "unknown"
    strongest_visible_strength: str = Field(min_length=1, max_length=240)
    strongest_visible_flaw: str = Field(min_length=1, max_length=240)
    hard_disqualifiers: list[str] = Field(max_length=8)
    observation_confidence: int = Field(default=0, ge=0, le=100)

    @field_validator("hard_disqualifiers")
    @classmethod
    def validate_hard_disqualifiers(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item or len(item) > 160:
                raise ValueError("hard disqualifiers must contain 1 to 160 characters")
            normalized.append(item)
        return normalized


class PrivacyObservationV1(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    sensitivity_class: Literal[
        "safe",
        "suggestive",
        "partial_nudity",
        "topless",
        "nudity",
        "genital_focus",
        "erotic",
        "sexual_activity",
        "intimate_nonsexual",
        "medical",
        "uncertain_sensitive",
    ]
    visible_sensitive_evidence: str = Field(max_length=240)
    nudity_level: Literal["none", "partial", "topless", "nude", "explicit", "unknown"]
    sexual_or_erotic_context: Literal["no", "yes", "uncertain"]
    intimate_nonsexual_context: Literal["no", "yes", "uncertain"]
    privacy_confidence: int = Field(ge=0, le=100)
    privacy_route: Literal["normal", "blur_and_qwen", "private_review"]

    @model_validator(mode="after")
    def validate_privacy_route(self) -> PrivacyObservationV1:
        sensitive = self.sensitivity_class != "safe"
        if sensitive and self.privacy_route == "normal":
            raise ValueError("sensitive observations cannot use the normal privacy route")
        if not sensitive and self.privacy_route != "normal":
            raise ValueError("safe observations must use the normal privacy route")
        if not sensitive and self.visible_sensitive_evidence.strip():
            raise ValueError("safe observations cannot claim visible sensitive evidence")
        if not sensitive and self.nudity_level != "none":
            raise ValueError("safe observations cannot claim a nudity level")
        if not sensitive and self.sexual_or_erotic_context != "no":
            raise ValueError("safe observations cannot claim sexual or erotic context")
        if not sensitive and self.intimate_nonsexual_context != "no":
            raise ValueError("safe observations cannot claim intimate context")
        return self


class SemanticDimensionsV1(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    purpose_and_intentional_success: int = Field(ge=0, le=100)
    subject_success: int = Field(ge=0, le=100)
    timing_or_visible_peak: int = Field(ge=0, le=100)
    composition_and_framing: int = Field(ge=0, le=100)
    emotional_or_atmospheric_impact: int = Field(ge=0, le=100)
    archive_and_documentary_usefulness: int = Field(ge=0, le=100)
    uniqueness: int = Field(ge=0, le=100)


class IndividualPhotoReviewV2(ApiSchema):
    """Structured individual-photo proposal returned by Ministral or advisory Qwen.

    Evidence stages (media staging, similarity embedding) deliberately do not
    implement this contract and therefore cannot create a star proposal.
    """

    model_config = ConfigDict(extra="forbid")

    observations: PhotoObservationsV1
    privacy: PrivacyObservationV1
    dimensions: SemanticDimensionsV1
    rating: int = Field(ge=1, le=5)
    rating_confidence: int = Field(ge=0, le=100)
    rating_rationale: str = Field(min_length=1, max_length=600)
    hard_critique: str = Field(min_length=1, max_length=400)
    why_not_five: str = Field(max_length=400)
    sensitivity: Literal[
        "safe",
        "suggestive",
        "partial_nudity",
        "topless",
        "nudity",
        "genital_focus",
        "erotic",
        "sexual_activity",
        "intimate_nonsexual",
        "medical",
        "uncertain_sensitive",
    ]
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    description: str = Field(min_length=1, max_length=1200)
    content_tags: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_observation_key(cls, value: object) -> object:
        # Retained payloads written before the primary_review rename keep the
        # old key; accept them without mutating stored history.
        if isinstance(value, dict) and "dossier" in value and "observations" not in value:
            value = dict(value)
            value["observations"] = value.pop("dossier")
        return value

    @model_validator(mode="after")
    def validate_individual_rating(self) -> IndividualPhotoReviewV2:
        if self.sensitivity != self.privacy.sensitivity_class:
            raise ValueError("sensitivity must match the privacy observation")
        if self.rating == 5 and self.why_not_five.strip():
            raise ValueError("a 5-star review cannot include why_not_five")
        if self.rating < 5 and not self.why_not_five.strip():
            raise ValueError("ratings below 5 must explain why_not_five")
        return self


class PhotoObservationModelOutput(IndividualPhotoReviewV2):
    """Compatibility name for the current individual-photo review schema."""


STAR_FLAGS: tuple[str, ...] = (
    # What costs a star.
    "accidental_frame",
    "nothing_happening",
    "closed_eyes",
    "face_turned_away",
    "face_blurred",
    "motion_blur",
    "out_of_focus",
    "badly_exposed",
    "edge_intrusion",
    "bad_crop",
    "face_cut_off",
    "obstructed",
    "reflection_or_glare",
    "repeated_frame",
    # What earns one.
    "real_interaction",
    "peak_moment",
    "strong_expression",
    "whole_family",
    "child_growing_up",
    "travel_or_place",
    "vivid_color_or_light",
    "clean_composition",
    "deliberate_selective_focus",
    "print_worthy",
)
NEGATIVE_STAR_FLAGS: frozenset[str] = frozenset(STAR_FLAGS[:14])
POSITIVE_STAR_FLAGS: frozenset[str] = frozenset(STAR_FLAGS[14:])

StarFlag = Literal[
    "accidental_frame",
    "nothing_happening",
    "closed_eyes",
    "face_turned_away",
    "face_blurred",
    "motion_blur",
    "out_of_focus",
    "badly_exposed",
    "edge_intrusion",
    "bad_crop",
    "face_cut_off",
    "obstructed",
    "reflection_or_glare",
    "repeated_frame",
    "real_interaction",
    "peak_moment",
    "strong_expression",
    "whole_family",
    "child_growing_up",
    "travel_or_place",
    "vivid_color_or_light",
    "clean_composition",
    "deliberate_selective_focus",
    "print_worthy",
]

# The sortable tag vocabulary a rating model may answer with. It is the only
# thing the JSON schema lets grammar-constrained decoding emit for ``tags``,
# and it maps 1:1 onto the ``star/<tag>`` names write-back exports to Immich.
# Owner-only tags (edit, print, private, sensitive) are deliberately absent:
# a model never proposes them.
StarTag = Literal[
    "family-moment",
    "candid",
    "portrait",
    "group",
    "child",
    "baby",
    "pet",
    "travel",
    "documentary",
    "landscape",
    "cityscape",
    "architecture",
    "nature",
    "beach",
    "snow",
    "food",
    "celebration",
    "holiday",
    "sport",
    "vehicle",
    "indoor",
    "outdoor",
    "night",
    "sunset",
    "selfie",
    "screenshot",
    "document",
    "artwork",
    "event",
    "home",
    "school",
    "work",
]
STAR_TAGS: tuple[str, ...] = (
    "family-moment",
    "candid",
    "portrait",
    "group",
    "child",
    "baby",
    "pet",
    "travel",
    "documentary",
    "landscape",
    "cityscape",
    "architecture",
    "nature",
    "beach",
    "snow",
    "food",
    "celebration",
    "holiday",
    "sport",
    "vehicle",
    "indoor",
    "outdoor",
    "night",
    "sunset",
    "selfie",
    "screenshot",
    "document",
    "artwork",
    "event",
    "home",
    "school",
    "work",
)
MAX_STAR_TAGS = 5

SensitivityClass = Literal[
    "safe",
    "suggestive",
    "partial_nudity",
    "topless",
    "nudity",
    "genital_focus",
    "erotic",
    "sexual_activity",
    "intimate_nonsexual",
    "medical",
    "uncertain_sensitive",
]


class StarRatingV1(ApiSchema):
    """The only thing a rating model is asked for: a star, and the little that
    keeps privacy, series selection, and search working.

    No prose review, no dimensions, no dossier. Flags name what the owner
    marks down or values; ``summary`` is one short searchable line; ``tags``
    feed search and write-back. About one hundred output tokens.
    """

    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)
    confidence: int = Field(ge=0, le=100)
    sensitivity: SensitivityClass
    privacy_note: str = Field(default="", max_length=120)
    people: int = Field(ge=0, le=100)
    flags: list[StarFlag] = Field(default_factory=list, max_length=8)
    summary: str = Field(min_length=1, max_length=120)
    tags: list[StarTag] = Field(default_factory=list, max_length=MAX_STAR_TAGS)

    @field_validator("flags")
    @classmethod
    def validate_flags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, values: object) -> object:
        """Keep only vocabulary tags, lower-cased and unique.

        Unknown strings are dropped rather than rejected so an older worker's
        free-text answer still validates; the schema served to the model
        carries the enum, so a grammar-constrained runtime never emits them.
        """

        if not isinstance(values, list):
            return values
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            item = " ".join(value.strip().lower().replace("_", "-").split()).replace(" ", "-")
            if item in STAR_TAGS and item not in normalized:
                normalized.append(item)
            if len(normalized) == MAX_STAR_TAGS:
                break
        return normalized

    @model_validator(mode="after")
    def validate_privacy_note(self) -> StarRatingV1:
        if self.sensitivity == "safe":
            self.privacy_note = ""
        return self


class StarRatingResultSubmission(StarRatingV1):
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0, le=3_600_000)
    media_provenance: MediaProvenance
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)


class FastFamilyPhotoReviewOutput(ApiSchema):
    """Lean first-pass contract for the in-app production-path benchmark."""

    model_config = ConfigDict(extra="forbid")

    visible_summary: str = Field(min_length=1, max_length=400)
    technical_quality: ObservationQuality
    moment_quality: ObservationQuality
    composition_quality: ObservationQuality
    visible_people_count: int = Field(ge=0, le=100)
    people_or_faces_limited: bool
    strongest_visible_strength: str = Field(min_length=1, max_length=160)
    strongest_visible_flaw: str = Field(min_length=1, max_length=160)
    privacy: PrivacyObservationV1
    rating: int = Field(ge=1, le=5)
    rating_confidence: int = Field(ge=0, le=100)
    rating_rationale: str = Field(min_length=1, max_length=240)
    sensitivity: Literal[
        "safe",
        "suggestive",
        "partial_nudity",
        "topless",
        "nudity",
        "genital_focus",
        "erotic",
        "sexual_activity",
        "intimate_nonsexual",
        "medical",
        "uncertain_sensitive",
    ]
    uncertainties: list[str] = Field(default_factory=list, max_length=4)
    content_tags: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_fast_review(self) -> FastFamilyPhotoReviewOutput:
        if self.sensitivity != self.privacy.sensitivity_class:
            raise ValueError("sensitivity must match the privacy observation")
        return self


class FastFamilyPhotoReviewResultSubmission(FastFamilyPhotoReviewOutput):
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    # Benchmarks share the production 20-minute cumulative hang guard. Their
    # speed target is evaluated from measured latency, not by rejecting a valid
    # late result at the API schema boundary.
    duration_ms: int = Field(ge=0, le=1_200_000)
    media_provenance: MediaProvenance
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=2)


class DeepReviewPatch(ApiSchema):
    """Small set of visible facts Qwen may independently correct."""

    model_config = ConfigDict(extra="forbid")

    visible_people_count: int = Field(ge=0, le=100)
    face_visibility: ObservationVisibility
    gaze_visibility: ObservationVisibility
    expression_visibility: ObservationVisibility
    eye_state: Literal[
        "open",
        "accidental_closed",
        "intentional_closed",
        "mixed",
        "unknown",
        "not_applicable",
    ]
    timing_quality: ObservationQuality
    subject_focus: ObservationQuality
    composition_hierarchy: ObservationQuality
    strongest_visible_strength: str = Field(min_length=1, max_length=240)
    strongest_visible_flaw: str = Field(min_length=1, max_length=240)


class DeepVisualObservationOutput(ApiSchema):
    """Compact blind Qwen pass used before image-free reconciliation."""

    model_config = ConfigDict(extra="forbid")

    visible_summary: str = Field(min_length=1, max_length=600)
    review_patch: DeepReviewPatch
    privacy: PrivacyObservationV1
    rating: int = Field(ge=1, le=5)
    rating_confidence: int = Field(ge=0, le=100)
    rating_rationale: str = Field(min_length=1, max_length=400)
    hard_critique: str = Field(min_length=1, max_length=300)
    why_not_five: str = Field(max_length=300)
    sensitivity: Literal[
        "safe",
        "suggestive",
        "partial_nudity",
        "topless",
        "nudity",
        "genital_focus",
        "erotic",
        "sexual_activity",
        "intimate_nonsexual",
        "medical",
        "uncertain_sensitive",
    ]
    uncertainties: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_patch_key(cls, value: object) -> object:
        # Durable checkpoints written before the primary_review rename keep the
        # old key; accept them without mutating stored history.
        if isinstance(value, dict) and "dossier_patch" in value and "review_patch" not in value:
            value = dict(value)
            value["review_patch"] = value.pop("dossier_patch")
        return value

    @model_validator(mode="after")
    def validate_blind_adjudication(self) -> DeepVisualObservationOutput:
        if self.sensitivity != self.privacy.sensitivity_class:
            raise ValueError("sensitivity must match the privacy observation")
        if self.rating == 5 and self.why_not_five.strip():
            raise ValueError("a 5-star review cannot include why_not_five")
        if self.rating < 5 and not self.why_not_five.strip():
            raise ValueError("ratings below 5 must explain why_not_five")
        return self


class PhotoReviewReconciliationOutput(ApiSchema):
    """Compact adjudication applied to an already accepted visual review.

    Historical reconciliation uses this shape without an image. The three-pass
    deep-curation experiment instead resends the image for a fresh final visual
    verdict, while retaining this bounded patch contract rather than regenerating
    the complete review.
    """

    model_config = ConfigDict(extra="forbid")

    review_patch: DeepReviewPatch
    privacy: PrivacyObservationV1
    rating: int = Field(ge=1, le=5)
    rating_confidence: int = Field(ge=0, le=100)
    rating_rationale: str = Field(min_length=1, max_length=600)
    hard_critique: str = Field(min_length=1, max_length=400)
    why_not_five: str = Field(max_length=400)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_patch_key(cls, value: object) -> object:
        # Retained reconciliation payloads keep the pre-rename key.
        if isinstance(value, dict) and "dossier_patch" in value and "review_patch" not in value:
            value = dict(value)
            value["review_patch"] = value.pop("dossier_patch")
        return value

    sensitivity: Literal[
        "safe",
        "suggestive",
        "partial_nudity",
        "topless",
        "nudity",
        "genital_focus",
        "erotic",
        "sexual_activity",
        "intimate_nonsexual",
        "medical",
        "uncertain_sensitive",
    ]
    uncertainties: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> PhotoReviewReconciliationOutput:
        if self.sensitivity != self.privacy.sensitivity_class:
            raise ValueError("sensitivity must match the privacy observation")
        if self.rating == 5 and self.why_not_five.strip():
            raise ValueError("a 5-star review cannot include why_not_five")
        if self.rating < 5 and not self.why_not_five.strip():
            raise ValueError("ratings below 5 must explain why_not_five")
        return self


class DeepEvidenceAuditOutput(PhotoReviewReconciliationOutput):
    """Image-free audit of a blind deep-curation report against retained evidence.

    This deliberately remains a compact adjudication rather than a second full
    review.  It can identify corroboration and disagreement in source-labelled
    evidence, but it never receives or represents the owner's rating.
    """

    corroborated_findings: list[PreferenceInsight180] = Field(
        default_factory=list,
        max_length=8,
    )
    disputed_findings: list[PreferenceInsight180] = Field(
        default_factory=list,
        max_length=8,
    )
    evidence_limits: list[PreferenceInsight180] = Field(
        default_factory=list,
        max_length=8,
    )
    family_context_findings: list[PreferenceInsight180] = Field(
        default_factory=list,
        max_length=6,
    )
    provenance_findings: list[PreferenceInsight180] = Field(
        default_factory=list,
        max_length=4,
    )

    @field_validator(
        "corroborated_findings",
        "disputed_findings",
        "evidence_limits",
        "family_context_findings",
        "provenance_findings",
    )
    @classmethod
    def validate_bounded_findings(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 180 for value in values):
            raise ValueError("deep evidence findings must contain 1 to 180 characters")
        return values


class PhotoObservationResultSubmission(IndividualPhotoReviewV2):
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0, le=3_600_000)
    media_provenance: MediaProvenance
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)


class AssetVlmResultSubmission(ApiSchema):
    model_config = ConfigDict(extra="forbid")

    media_provenance: MediaProvenance

    proposed_rating: int = Field(ge=1, le=5)
    technical_verdict: Literal["reject", "poor", "acceptable", "good", "excellent"]
    focus_quality: float = Field(ge=0, le=1)
    exposure_quality: float = Field(ge=0, le=1)
    composition_quality: float = Field(ge=0, le=1)
    subject_quality: float = Field(ge=0, le=1)
    artistic_merit: float = Field(ge=0, le=1)
    originality: float = Field(ge=0, le=1)
    documentary_value: float = Field(ge=0, le=1)
    privacy_risk: float | None = Field(default=None, ge=0, le=1)
    sensitive_content: (
        Literal[
            "none",
            "suggestive",
            "sexually_explicit",
            "intimate",
            "nude",
            "sexual",
            "erotic",
            "boudoir",
            "uncertain",
        ]
        | None
    ) = None
    photographic_verdict: Literal[
        "empty_snapshot",
        "failed_snapshot",
        "documentary_record",
        "competent_photo",
        "strong_photo",
        "exceptional_photo",
        "art_candidate",
    ]
    harsh_critique: str = Field(min_length=1, max_length=10_000)
    description: str = Field(min_length=1)
    content_tags: list[str] = Field(default_factory=list, max_length=5)
    family_value: float = Field(ge=0, le=1)
    emotional_value: float = Field(ge=0, le=1)
    story_value: float = Field(ge=0, le=1)
    # Keep the legacy persisted projection aligned with the current structured
    # observation contract. This is a bounded visible count, not an identity or
    # relationship inference.
    visible_people_count: int = Field(default=0, ge=0, le=100)
    has_children: bool = False
    has_closed_eyes: bool = False
    print_value: float = Field(default=0, ge=0, le=1)
    display_value: float = Field(default=0, ge=0, le=1)
    publication_value: float = Field(default=0, ge=0, le=1)
    total_blur: bool
    motion_blur: bool
    defocus: bool
    accidental_photo_probability: float = Field(ge=0, le=1)
    deletion_review_candidate: bool
    preserve_despite_technical_faults: bool
    recoverability: Literal["none", "low", "moderate", "high"]
    editability: Literal["none", "limited", "worth_editing", "strong_candidate"]
    recovery_actions: list[
        Literal[
            "crop",
            "straighten",
            "exposure",
            "white_balance",
            "color",
            "noise_reduction",
            "sharpen",
            "deblur",
            "local_contrast",
            "subject_mask",
        ]
    ] = Field(default_factory=list, max_length=8)
    recovery_explanation: str = Field(min_length=1, max_length=10_000)
    tone_advice: dict[
        Literal[
            "exposure",
            "contrast",
            "highlights",
            "shadows",
            "whites",
            "blacks",
            "saturation",
            "warmth",
        ],
        Literal["lower_strong", "lower", "hold", "raise", "raise_strong"],
    ] = Field(default_factory=dict, max_length=8)
    crop_advice: str = Field(min_length=1, max_length=10_000)
    editing_explanation: str = Field(min_length=1, max_length=10_000)
    restoration_strategy: Literal[
        "not_needed",
        "conservative_restoration_candidate",
        "generative_restoration_candidate",
        "not_recommended",
    ]
    restoration_risk: Literal["low", "medium", "high"]
    recovery_confidence: float = Field(default=0.5, ge=0, le=1)
    strengths: list[str] = Field(default_factory=list, max_length=8)
    weaknesses: list[str] = Field(default_factory=list, max_length=8)
    uncertainty: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=10_000)
    final_summary: str = Field(default="", max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    roast_text: str = Field(default="", max_length=10_000)
    roast_reason: str = Field(default="", max_length=10_000)
    is_actually_trash: bool = False
    deep_review_version: str = Field(default="", max_length=128)
    what_this_records: str = Field(default="", max_length=10_000)
    why_it_matters: str = Field(default="", max_length=10_000)
    why_it_may_not: str = Field(default="", max_length=10_000)
    rescue_and_role: str = Field(default="", max_length=10_000)
    collection_role: (
        Literal[
            "highlight",
            "strong_keeper",
            "ordinary_keeper",
            "family_record",
            "documentary_record",
            "event_context",
            "reference_image",
            "failed_frame",
            "unknown_without_collection_context",
        ]
        | None
    ) = None
    why_not_five: str = Field(default="", max_length=10_000)
    why_five: str = Field(default="", max_length=10_000)
    content_interest: float | None = Field(default=None, ge=0, le=1)
    revisit_value: float | None = Field(default=None, ge=0, le=1)
    evidence_density: float | None = Field(default=None, ge=0, le=1)
    uniqueness_potential: float | None = Field(default=None, ge=0, le=1)
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0, le=3_600_000)
    inference_runs: list[InferenceInvocationSubmission] = Field(default_factory=list, max_length=4)
    independent_visual_report: PhotoObservationModelOutput | None = None
    evidence_audit_report: DeepEvidenceAuditOutput | None = None
    final_verdict_report: PhotoReviewReconciliationOutput | None = None
    final_review_report: PhotoObservationModelOutput | None = None

    @field_validator("strengths", "weaknesses", "warnings")
    @classmethod
    def validate_asset_observations(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("VLM observations must contain 1 to 256 characters")
        return value

    @field_validator("content_tags")
    @classmethod
    def validate_content_tags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = " ".join(item.strip().lower().split())
            if not normalized or len(normalized) > 48:
                raise ValueError("content tags must contain 1 to 48 characters")
            if normalized not in seen:
                tags.append(normalized)
                seen.add(normalized)
        return tags

    @field_validator("tone_advice")
    @classmethod
    def validate_complete_tone_advice(cls, value: dict[str, str]) -> dict[str, str]:
        expected = {
            "exposure",
            "contrast",
            "highlights",
            "shadows",
            "whites",
            "blacks",
            "saturation",
            "warmth",
        }
        if set(value) != expected:
            raise ValueError("tone advice must include every supported editing direction")
        return value

    @model_validator(mode="after")
    def validate_deep_review_sections(self) -> AssetVlmResultSubmission:
        if not self.deep_review_version:
            return self
        required_text = {
            "roast_text": self.roast_text,
            "what_this_records": self.what_this_records,
            "why_it_matters": self.why_it_matters,
            "why_it_may_not": self.why_it_may_not,
            "rescue_and_role": self.rescue_and_role,
        }
        missing = [name for name, value in required_text.items() if not value.strip()]
        if missing:
            raise ValueError("deep review is missing required sections: " + ", ".join(missing))
        if self.collection_role is None:
            raise ValueError("deep review must choose a collection role")
        if self.proposed_rating == 5:
            if not self.why_five.strip() or self.why_not_five.strip():
                raise ValueError("a 5-star deep review requires why_five only")
        elif not self.why_not_five.strip() or self.why_five.strip():
            raise ValueError("a sub-5-star deep review requires why_not_five only")
        if None in {
            self.content_interest,
            self.revisit_value,
            self.evidence_density,
            self.uniqueness_potential,
        }:
            raise ValueError("deep review dimension scores are required")
        return self


class DeepCurationRuntimeIdentity(ApiSchema):
    """Inventory identity required before a deep-curation pass may be reused."""

    model_config = ConfigDict(extra="forbid")

    model_key: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    quantization: str = Field(min_length=1, max_length=128)
    runtime_format: str = Field(min_length=1, max_length=128)
    runtime_version: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_checksum: str | None = Field(default=None, min_length=1, max_length=256)


class DeepCurationCheckpoint(ApiSchema):
    """Validated three-pass outputs retained across a worker retry or restart.

    The coordinator stores this document as opaque task checkpoint state.  A
    worker still has to match every identity and request digest before it may
    reuse any pass; a valid shape alone is deliberately insufficient.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["deep-curation-three-pass-v1"]
    task_id: str = Field(min_length=1, max_length=128)
    task_type: Literal["asset.deep_review_experiment"]
    asset_revision_id: str | None = Field(default=None, max_length=128)
    media_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_key: str = Field(min_length=1, max_length=256)
    model_identity: DeepCurationRuntimeIdentity | None = None
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    blind_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    blind_report: PhotoObservationModelOutput
    blind_inference: InferenceInvocationSubmission

    evidence_input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_audit_report: DeepEvidenceAuditOutput | None = None
    evidence_inference: InferenceInvocationSubmission | None = None

    final_input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_verdict_report: PhotoReviewReconciliationOutput | None = None
    final_inference: InferenceInvocationSubmission | None = None

    @model_validator(mode="after")
    def validate_completed_pass_groups(self) -> DeepCurationCheckpoint:
        evidence_values = (
            self.evidence_input_digest,
            self.evidence_audit_report,
            self.evidence_inference,
        )
        if any(value is not None for value in evidence_values) and not all(
            value is not None for value in evidence_values
        ):
            raise ValueError("deep-curation evidence checkpoint fields must be complete")
        final_values = (
            self.final_input_digest,
            self.final_verdict_report,
            self.final_inference,
        )
        if any(value is not None for value in final_values) and not all(
            value is not None for value in final_values
        ):
            raise ValueError("deep-curation final checkpoint fields must be complete")
        if self.final_input_digest is not None and self.evidence_input_digest is None:
            raise ValueError("deep-curation final checkpoint requires the evidence pass")
        invocations = [
            invocation
            for invocation in (
                self.blind_inference,
                self.evidence_inference,
                self.final_inference,
            )
            if invocation is not None
        ]
        if len({item.model_reported_version for item in invocations}) > 1:
            raise ValueError("deep-curation passes must use one reported model version")
        if len({item.runtime for item in invocations}) > 1:
            raise ValueError("deep-curation passes must use one model runtime")
        return self


class AssetVlmEnsembleReviewRequest(ApiSchema):
    model_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    review_profile: Literal["curator", "art_critic", "edit_advisor", "roaster"] = "curator"

    @field_validator("model_version_ids")
    @classmethod
    def unique_asset_models(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("ensemble model versions must be unique")
        return value


class WorkerTaskFailure(ApiSchema):
    error_class: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    retryable: bool = True
    reason: str | None = Field(default=None, max_length=600)
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=128)
    schema_version: str | None = Field(default=None, min_length=1, max_length=128)
    source_asset_id: uuid.UUID | None = None
    source_error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-z0-9_]+$",
    )
