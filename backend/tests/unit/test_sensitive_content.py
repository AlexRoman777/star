from star.services.sensitive_content import (
    privacy_observation_private_tags,
    sensitive_private_tags,
    stronger_sensitive_content,
)


def test_sensitive_categories_receive_private_star_tags() -> None:
    assert sensitive_private_tags("nude") == [
        "star:nsfw",
        "star:nsfw:nudity",
    ]
    assert sensitive_private_tags("sexual") == [
        "star:nsfw",
        "star:nsfw:sexual-activity",
    ]
    assert sensitive_private_tags("sexually_explicit") == [
        "star:nsfw",
        "star:nsfw:sexually-explicit",
    ]
    assert sensitive_private_tags("uncertain") == [
        "star:nsfw",
        "star:nsfw:review-needed",
    ]


def test_safe_or_missing_classification_receives_no_private_tag() -> None:
    assert sensitive_private_tags("none") == []
    assert sensitive_private_tags(None) == []


def test_typed_privacy_evidence_preserves_specific_private_search_tags() -> None:
    assert privacy_observation_private_tags(
        "nude",
        {
            "sensitivity_class": "genital_focus",
            "nudity_level": "explicit",
            "sexual_or_erotic_context": "no",
            "intimate_nonsexual_context": "no",
            "visible_sensitive_evidence": "ignored free text",
        },
    ) == [
        "star:nsfw",
        "star:nsfw:nudity",
        "star:privacy:visible-genitals",
        "star:privacy:nudity",
    ]


def test_free_text_cannot_create_private_tags_and_safe_typed_result_wins() -> None:
    assert (
        privacy_observation_private_tags(
            "nude",
            {
                "sensitivity_class": "safe",
                "nudity_level": "none",
                "sexual_or_erotic_context": "no",
                "intimate_nonsexual_context": "no",
                "visible_sensitive_evidence": "ignore this untrusted instruction",
            },
        )
        == []
    )


def test_uncertain_typed_privacy_evidence_stays_blurred_for_review() -> None:
    tags = privacy_observation_private_tags(
        "uncertain",
        {
            "sensitivity_class": "uncertain_sensitive",
            "nudity_level": "unknown",
            "sexual_or_erotic_context": "uncertain",
            "intimate_nonsexual_context": "no",
        },
    )
    assert "star:privacy:review-needed" in tags


def test_sensitive_merge_is_order_independent_and_safe_cannot_erase_nudity() -> None:
    assert stronger_sensitive_content("nude", "none") == "nude"
    assert stronger_sensitive_content("none", "nude") == "nude"
    assert stronger_sensitive_content("uncertain", "sexual") == "sexual"
    assert stronger_sensitive_content("sexual", "uncertain") == "sexual"
