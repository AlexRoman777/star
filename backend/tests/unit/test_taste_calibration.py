from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from star.db.models import LibraryProgram
from star.services.review_prompts import with_taste_calibration
from star.services.taste_calibration import (
    CALIBRATION_HEADING,
    MAXIMUM_CALIBRATION_CHARACTERS,
    MINIMUM_LABELS,
    OwnerCorrection,
    refresh_taste_calibration,
    render_calibration,
    taste_calibration_text,
)


def _correction(owner: int, model: int, summary: str, *flags: str, days_ago: int = 0):
    return OwnerCorrection(
        asset_id=uuid.uuid4(),
        owner_rating=owner,
        model_rating=model,
        summary=summary,
        flags=tuple(flags),
        decided_at=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(days=days_ago),
    )


def _generous_model_corrections() -> list[OwnerCorrection]:
    corrections = []
    for index in range(4):
        corrections.append(_correction(1, 2, f"blurry child running {index}", "out_of_focus"))
        corrections.append(_correction(2, 3, f"empty living room {index}", "nothing_happening"))
        corrections.append(_correction(4, 3, f"two children laughing {index}", "real_interaction"))
    corrections.append(_correction(3, 3, "birthday table", "travel_or_place"))
    corrections.append(_correction(5, 4, "whole family on the beach at sunset", "whole_family"))
    return corrections


def test_calibration_stays_silent_below_the_label_floor() -> None:
    rendered = render_calibration([_correction(1, 2, "x")] * (MINIMUM_LABELS - 1))

    assert rendered["text"] == ""
    assert rendered["label_count"] == MINIMUM_LABELS - 1


def test_calibration_names_the_bias_examples_and_flags() -> None:
    rendered = render_calibration(_generous_model_corrections())
    text = rendered["text"]

    assert rendered["label_count"] == 14
    assert rendered["exact_agreement"] == round(1 / 14, 3)
    assert "owner 1-star photos were rated 2.0 on average" in text
    assert "Be stricter at the bottom" in text
    assert "Be readier to give 4 or 5" in text
    assert 'owner 4 but model 3: "two children laughing' in text
    assert "1-2 star photos: out of focus, nothing happening" in text
    assert "4-5 star photos: real interaction, whole family" in text
    assert len(text) <= MAXIMUM_CALIBRATION_CHARACTERS
    assert "asset" not in text.lower()


def test_calibration_is_bounded_even_with_long_summaries() -> None:
    corrections = [
        _correction(1, 3, "word " * 60, "out_of_focus", days_ago=index) for index in range(40)
    ]

    rendered = render_calibration(corrections)

    assert len(rendered["text"]) <= MAXIMUM_CALIBRATION_CHARACTERS


def test_taste_prompt_appends_the_learned_block_only_when_present() -> None:
    program = LibraryProgram(name="calibration", model_pipeline_policy={})
    assert with_taste_calibration("Owner text.", program) == "Owner text."
    assert taste_calibration_text(program) == ""

    program.model_pipeline_policy = {
        "taste_calibration": {"text": "Be stricter.", "label_count": 12}
    }
    combined = with_taste_calibration("Owner text.", program)

    assert combined == f"Owner text.\n\n{CALIBRATION_HEADING}\nBe stricter."


async def test_refresh_waits_for_enough_new_corrections(session) -> None:
    program = LibraryProgram(name="refresh", model_pipeline_policy={})
    session.add(program)
    await session.flush()
    now = datetime(2026, 9, 3, tzinfo=UTC)

    changed = await refresh_taste_calibration(session, program=program, now=now)

    # The first pass records the (empty) block so later passes can compare.
    assert changed is True
    stored = program.model_pipeline_policy["taste_calibration"]
    assert stored["label_count"] == 0
    assert stored["text"] == ""
    assert stored["generated_at"] == now.isoformat()

    # Nothing new: the stored block is kept and the timestamp is not churned.
    assert (
        await refresh_taste_calibration(session, program=program, now=now + timedelta(hours=1))
        is False
    )
    assert program.model_pipeline_policy["taste_calibration"]["generated_at"] == now.isoformat()
