from __future__ import annotations

from typing import get_args

import pytest

from star.agent._base import ASSET_VLM_SCHEMA
from star.domain.schemas import StarRatingV1
from star.domain.schemas.submissions import MAX_STAR_TAGS, STAR_TAGS, StarTag
from star.services.writeback import CONTROLLED_TAGS, OWNER_TAGS, TAG_ALIASES

OWNER_ONLY = {"edit", "print", "private", "sensitive"}


def _star(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "rating": 3,
        "confidence": 70,
        "sensitivity": "safe",
        "privacy_note": "",
        "people": 1,
        "flags": [],
        "summary": "A child on a beach.",
        "tags": [],
    }
    document.update(overrides)
    return document


def test_vocabulary_is_the_sortable_list_without_owner_tags() -> None:
    assert tuple(get_args(StarTag)) == STAR_TAGS
    assert len(STAR_TAGS) == 32
    assert len(set(STAR_TAGS)) == len(STAR_TAGS)
    assert not OWNER_ONLY & set(STAR_TAGS)
    assert OWNER_TAGS == ("edit", "print", "private", "sensitive")
    for expected in (
        "family-moment",
        "baby",
        "cityscape",
        "screenshot",
        "document",
        "selfie",
        "school",
        "work",
    ):
        assert expected in STAR_TAGS


def test_model_schema_carries_the_tag_enum_for_grammar_constrained_decoding() -> None:
    tags = ASSET_VLM_SCHEMA["properties"]["tags"]
    assert tags["type"] == "array"
    assert tags["maxItems"] == MAX_STAR_TAGS
    assert tags["items"] == {"type": "string", "enum": list(STAR_TAGS)}


def test_unknown_or_owner_tags_are_dropped_not_rejected() -> None:
    rated = StarRatingV1.model_validate(
        _star(tags=["Beach", "children", "kitchen", "private", "beach", "Family_Moment"])
    )
    assert rated.tags == ["beach", "family-moment"]

    older_worker = StarRatingV1.model_validate(_star(tags=["couch", "window", "people"]))
    assert older_worker.tags == []


def test_tags_are_capped_at_five_after_normalisation() -> None:
    rated = StarRatingV1.model_validate(
        _star(tags=["beach", "junk", "child", "pet", "outdoor", "sunset", "travel", "snow"])
    )
    assert rated.tags == ["beach", "child", "pet", "outdoor", "sunset"]
    with pytest.raises(ValueError):
        StarRatingV1.model_validate(_star(tags="beach"))


def test_writeback_vocabulary_matches_the_model_vocabulary_plus_owner_tags() -> None:
    assert (*STAR_TAGS, *OWNER_TAGS) == CONTROLLED_TAGS
    for tag in CONTROLLED_TAGS:
        assert TAG_ALIASES[tag] == tag
    assert set(TAG_ALIASES.values()) <= set(CONTROLLED_TAGS)
    assert TAG_ALIASES["family"] == "family-moment"
    assert TAG_ALIASES["children"] == "child"
    assert TAG_ALIASES["kid"] == "child"
    assert TAG_ALIASES["dog"] == "pet"
    assert TAG_ALIASES["cat"] == "pet"
