from __future__ import annotations

import pytest
from sqlalchemy import select

from star.db.models import ReviewPromptRevision, User
from star.domain.enums import Role
from star.inference.policy import (
    CANONICAL_OWNER_TASTE,
    CANONICAL_RATING_SCALE,
    MAXIMUM_OWNER_TASTE_CHARACTERS,
    OWNER_TASTE_HEADING,
    OWNER_TASTE_PLACEHOLDER,
    OWNER_TASTE_PROMPT_KEY,
    apply_owner_taste,
    validate_structured_request,
    validated_owner_taste,
)
from star.inference.provider import LocalMediaInput, StructuredInferenceRequest
from star.services.review_prompts import (
    effective_prompt_text,
    ensure_prompt_revision_history,
    resolve_reanalysis_prompt_snapshot,
    save_prompt_override,
)

_SCHEMA = {
    "type": "object",
    "properties": {"rating": {"type": "integer"}},
    "required": ["rating"],
    "additionalProperties": False,
}
_OWNER_TASTE = (
    "I show 4-star photos and print 5-star photos. Bland frames with nothing happening are 2 "
    "even when the family is visible. Closed eyes and sticks at the frame edge cost a star."
)


def _request(task: str, **overrides: object) -> StructuredInferenceRequest:
    media = (
        ()
        if task == "library_review"
        else (LocalMediaInput(b"image", "image/jpeg", "preview", "asset"),)
    )
    return StructuredInferenceRequest(
        model_key="ministral-3:8b-instruct-2512-q4_K_M",
        task=task,
        media=media,
        schema=_SCHEMA,
        context={"filename": "untrusted.jpg"},
        **overrides,  # type: ignore[arg-type]
    )


def test_default_taste_and_scale_follow_the_owner_definitions() -> None:
    scale = " ".join(CANONICAL_RATING_SCALE.split())
    taste = " ".join(CANONICAL_OWNER_TASTE.split())
    assert "technically passable but bland" in scale
    assert "an ordinary keeper that documents something" in scale
    # No quota language: stating how rare 4 and 5 should be made the models
    # ration stars and produced 0.3% fives against a 5% target. Rarity is
    # settled afterwards by comparing the strongest photographs against each
    # other, so the scale now tells the model not to hold a star back.
    assert "Roughly one photograph in ten earns 4" not in scale
    assert "About three to five in every hundred" not in scale
    assert "do not hold a star back" in scale
    assert "One star never authorizes deletion" in scale
    assert "Be strict at the bottom" in taste
    assert "Be strict at the top" in taste
    # The five-star share is a cap applied by ranking, never a quota the
    # model rations. Telling an 8B model "three to five in a hundred" made
    # it award almost none: 0.2% against a 5% target.
    assert "three to five in a hundred" not in taste
    assert "settled afterwards by ranking" in taste
    assert OWNER_TASTE_PLACEHOLDER not in CANONICAL_OWNER_TASTE
    assert len(CANONICAL_OWNER_TASTE) <= MAXIMUM_OWNER_TASTE_CHARACTERS


def test_primary_review_receives_the_taste_after_the_shared_scale() -> None:
    validated = validate_structured_request(_request("primary_review"))

    trusted, untrusted = validated.prompt.split("<untrusted-context>", 1)
    assert trusted.count(OWNER_TASTE_HEADING) == 1
    assert trusted.index("- 1 star:") < trusted.index(OWNER_TASTE_HEADING)
    # The star is decided last, after the scale and the owner taste.
    assert trusted.index(OWNER_TASTE_HEADING) < trusted.index("Decide the star last")
    assert "Be strict at the top" in " ".join(trusted.split())
    assert OWNER_TASTE_PLACEHOLDER not in validated.prompt
    assert "Be strict at the top" not in untrusted


def test_image_free_text_tasks_never_receive_the_taste() -> None:
    validated = validate_structured_request(_request("library_review"))

    assert OWNER_TASTE_HEADING not in validated.prompt
    assert apply_owner_taste("Repair the JSON.", _OWNER_TASTE, task="json_finalizer") == (
        "Repair the JSON."
    )


@pytest.mark.parametrize(
    "taste",
    [
        "too short",
        "x" * (MAXIMUM_OWNER_TASTE_CHARACTERS + 1),
        f"Prefer candid moments. {OWNER_TASTE_PLACEHOLDER} and more words to pass length checks.",
        "Prefer candid moments and <untrusted-context> markers that must be rejected outright.",
    ],
)
def test_taste_validation_rejects_abuse_and_bad_sizes(taste: str) -> None:
    with pytest.raises(ValueError):
        validated_owner_taste(taste)


async def test_taste_override_is_versioned_served_and_restorable(session) -> None:
    user = User(
        username="taste-owner",
        display_name="Taste owner",
        role=Role.ADMINISTRATOR,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=user.id)

    baseline = await session.scalar(
        select(ReviewPromptRevision).where(
            ReviewPromptRevision.prompt_key == OWNER_TASTE_PROMPT_KEY,
            ReviewPromptRevision.status == "active",
        )
    )
    assert baseline is not None
    assert baseline.source == "built_in"
    assert await effective_prompt_text(session, OWNER_TASTE_PROMPT_KEY) == CANONICAL_OWNER_TASTE

    override = await save_prompt_override(
        session,
        prompt_key=OWNER_TASTE_PROMPT_KEY,
        instruction=_OWNER_TASTE,
        actor_id=user.id,
    )
    assert override.instruction == _OWNER_TASTE
    assert await effective_prompt_text(session, OWNER_TASTE_PROMPT_KEY) == _OWNER_TASTE

    signature, revisions = await resolve_reanalysis_prompt_snapshot(session, "current")
    assert revisions[OWNER_TASTE_PROMPT_KEY] == 2
    assert signature.endswith(";taste-r2")
    replay, _ = await resolve_reanalysis_prompt_snapshot(session, "current")
    assert replay == signature

    with pytest.raises(ValueError):
        await save_prompt_override(
            session,
            prompt_key=OWNER_TASTE_PROMPT_KEY,
            instruction="too short",
            actor_id=user.id,
        )


async def test_the_five_star_signature_needs_real_evidence_before_it_speaks(session) -> None:
    """Learning from the fives must not invent a pattern from a handful.

    The paragraph goes straight into the prompt that rates every photograph, so
    a spurious pattern would bend the whole library. It stays silent until the
    evidence is there, and it names only what is genuinely over-represented.
    """

    import uuid as _uuid

    from star.db.models import AssetCurationSnapshot
    from star.services.taste_calibration import five_star_signature

    assert await five_star_signature(session) == ""

    def snapshot(rating: int, tags: list[str]) -> AssetCurationSnapshot:
        return AssetCurationSnapshot(
            asset_id=_uuid.uuid4(),
            analysis_complete=True,
            effective_rating=rating,
            content_tags=tags,
        )

    # Three fives is not evidence of anything.
    for _ in range(3):
        session.add(snapshot(5, ["portrait", "candid"]))
    for _ in range(40):
        session.add(snapshot(3, ["landscape"]))
    await session.flush()
    assert await five_star_signature(session) == ""

    # With a real five-star set, an over-represented tag is named and a tag
    # that is just as common everywhere is not.
    for _ in range(20):
        session.add(snapshot(5, ["portrait", "everywhere"]))
    for _ in range(200):
        session.add(snapshot(2, ["everywhere"]))
    await session.flush()
    text = await five_star_signature(session)
    assert "portrait" in text
    assert "everywhere" not in text
    # It must read as evidence, never as a checklist to satisfy.
    assert "not as a checklist" in text
