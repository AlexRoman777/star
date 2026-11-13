from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from star.core.config import Settings, get_settings
from star.core.crypto import hash_session_token
from star.db.base import Base, utc_now
from star.db.models import Session, User
from star.db.session import get_session
from star.domain.enums import Role
from star.main import app


def media_provenance(
    *, asset_reference: str = "asset", delivered_variant: str = "fullsize"
) -> dict[str, object]:
    role = {
        "thumbnail": "720p_thumbnail",
        "preview": "4k_preview",
        "fullsize": "full_dimension_webp",
    }[delivered_variant]
    return {
        "contract_version": "immich-720p-thumbnail__4k-preview__full-dimension-webp-v1",
        "requested_variant": "fullsize",
        "delivered_variant": delivered_variant,
        "delivered_role": role,
        "source_media_type": "image/webp",
        "source_digest": "a" * 64,
        "source_width": 2048,
        "source_height": 1365,
        "visual_revision": "test-visual-revision",
        "fallback_reason": "fullsize_unavailable" if delivered_variant == "preview" else None,
        "contract_conformant": True,
        "asset_reference": asset_reference,
        "processing": {
            "mode": "exif_oriented_jpeg",
            "media_type": "image/jpeg",
            "digest": "b" * 64,
            "width": 2048,
            "height": 1365,
            "max_edge": 2048,
            "orientation_policy": "exif_transpose",
            "cropped": False,
            "upscaled": False,
        },
    }


def comparison_provenance(count: int) -> list[dict[str, object]]:
    return [media_provenance(asset_reference=f"candidate-{index}") for index in range(1, count + 1)]


def photo_observation_document(*, rating: int = 4) -> dict[str, object]:
    """Return one complete, schema-valid individual review without private content."""

    quality = "good"
    return {
        "observations": {
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
            "subject_interaction": quality,
            "pose_readiness": "ready",
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
            "background_distraction": "minor",
            "subject_background_separation": quality,
            "light_quality": quality,
            "color_quality": quality,
            "atmosphere_strength": quality,
            "visual_interest": quality,
            "meaningful_action_or_event": "present",
            "documentary_value": 78,
            "visible_archive_usefulness": 80,
            "practical_use": "share",
            "editing_recoverability": "high",
            "alternate_subject_strength": quality,
            "strongest_visible_strength": "A complete readable moment.",
            "strongest_visible_flaw": "The background is mildly distracting.",
            "hard_disqualifiers": [],
            "observation_confidence": 88,
        },
        "privacy": {
            "sensitivity_class": "safe",
            "visible_sensitive_evidence": "",
            "nudity_level": "none",
            "sexual_or_erotic_context": "no",
            "intimate_nonsexual_context": "no",
            "privacy_confidence": 98,
            "privacy_route": "normal",
        },
        "dimensions": {
            "purpose_and_intentional_success": 78,
            "subject_success": 80,
            "timing_or_visible_peak": 76,
            "composition_and_framing": 72,
            "emotional_or_atmospheric_impact": 75,
            "archive_and_documentary_usefulness": 80,
            "uniqueness": 68,
        },
        "rating": rating,
        "rating_confidence": 84,
        "rating_rationale": "The visible moment and archive value support the rating.",
        "hard_critique": "The background competes slightly with the subjects.",
        "why_not_five": "" if rating == 5 else "It is strong rather than exceptional.",
        "sensitivity": "safe",
        "uncertainties": [],
        "description": "Two people in a clearly visible posed moment.",
        "content_tags": ["people", "portrait"],
    }


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        public_origin="http://localhost",
        allowed_hosts=["localhost", "testserver"],
        cors_origins=["http://localhost"],
        encryption_key=Fernet.generate_key().decode("ascii"),
        session_secret="unit-test-session-secret",
        bootstrap_token="unit-test-bootstrap-token",
        artifact_root=tmp_path / "artifacts",
        storage_min_free_bytes=0,
    )


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as value:
        yield value


@pytest_asyncio.fixture
async def authenticated_context(session_factory, test_settings):
    token = "test-authentication-token"
    async with session_factory() as session:
        user = User(
            username="owner",
            display_name="Owner",
            role=Role.ADMINISTRATOR,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        auth_session = Session(
            user_id=user.id,
            token_hash=hash_session_token(token, test_settings.session_secret or ""),
            expires_at=utc_now() + timedelta(hours=1),
            reauthenticated_at=utc_now(),
        )
        session.add(auth_session)
        await session.commit()
        user_id = user.id

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as database_session:
            yield database_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        yield {"token": token, "user_id": user_id}
    finally:
        app.dependency_overrides.clear()
