from __future__ import annotations

from collections.abc import Mapping

SENSITIVE_CONTENT_VALUES = frozenset(
    {
        "suggestive",
        "sexually_explicit",
        "intimate",
        "nude",
        "sexual",
        "erotic",
        "boudoir",
        "uncertain",
        "owner_marked",
    }
)

_PRIVATE_TAG_SUFFIX = {
    "suggestive": "suggestive",
    "sexually_explicit": "sexually-explicit",
    "intimate": "intimate",
    "nude": "nudity",
    "sexual": "sexual-activity",
    "erotic": "erotic",
    "boudoir": "boudoir",
    "uncertain": "review-needed",
    "owner_marked": "owner-marked",
}

_SENSITIVE_CONTENT_PRECEDENCE = {
    "none": 0,
    "uncertain": 1,
    "intimate": 2,
    "suggestive": 3,
    "boudoir": 4,
    "erotic": 5,
    "nude": 6,
    "sexual": 7,
    "sexually_explicit": 8,
    "owner_marked": 9,
}


def stronger_sensitive_content(current: str | None, candidate: str | None) -> str | None:
    """Merge model privacy classes deterministically without allowing safe to erase risk."""

    current_rank = _SENSITIVE_CONTENT_PRECEDENCE.get(current or "", -1)
    candidate_rank = _SENSITIVE_CONTENT_PRECEDENCE.get(candidate or "", -1)
    return candidate if candidate_rank > current_rank else current


def sensitive_private_tags(category: object) -> list[str]:
    if not isinstance(category, str) or category not in SENSITIVE_CONTENT_VALUES:
        return []
    return ["star:nsfw", f"star:nsfw:{_PRIVATE_TAG_SUFFIX[category]}"]


_OBSERVATION_TAG_SUFFIX = {
    "suggestive": "suggestive",
    "partial_nudity": "partial-nudity",
    "topless": "exposed-breast",
    "nudity": "nudity",
    "genital_focus": "visible-genitals",
    "erotic": "erotic",
    "sexual_activity": "sexual-activity",
    "intimate_nonsexual": "intimate-nonsexual",
    "medical": "medical-private",
    "uncertain_sensitive": "review-needed",
}


def privacy_observation_private_tags(
    category: object,
    observation: Mapping[str, object] | None,
) -> list[str]:
    """Return bounded, machine-generated private tags from typed evidence only.

    The free-text evidence field is deliberately ignored. It may be useful to an
    owner reviewing the decision, but it must never invent an anatomical or sexual
    tag. These labels are privacy/search metadata only and have no rating effect.
    """

    tags = sensitive_private_tags(category)
    if observation is None:
        return tags

    sensitivity_class = observation.get("sensitivity_class")
    if sensitivity_class == "safe":
        return []
    if not isinstance(sensitivity_class, str):
        return tags

    suffix = _OBSERVATION_TAG_SUFFIX.get(sensitivity_class)
    if suffix is not None:
        tags.append(f"star:privacy:{suffix}")

    nudity_level = observation.get("nudity_level")
    if nudity_level in {"partial", "topless", "nude", "explicit"}:
        tags.append("star:privacy:nudity")
    if nudity_level == "explicit" or sensitivity_class == "genital_focus":
        tags.append("star:privacy:visible-genitals")
    if sensitivity_class == "topless":
        tags.append("star:privacy:exposed-breast")
    if observation.get("sexual_or_erotic_context") == "yes":
        tags.append("star:privacy:sexual-or-erotic")
    if observation.get("intimate_nonsexual_context") == "yes":
        tags.append("star:privacy:intimate-nonsexual")
    if (
        sensitivity_class == "uncertain_sensitive"
        or nudity_level == "unknown"
        or observation.get("sexual_or_erotic_context") == "uncertain"
        or observation.get("intimate_nonsexual_context") == "uncertain"
    ):
        tags.append("star:privacy:review-needed")

    return list(dict.fromkeys(tags))


def normalized_sensitive_content(details: Mapping[str, object]) -> str | None:
    """Reconcile the legacy class with the complete typed privacy observation."""

    raw_value = details.get("sensitive_content")
    raw = str(raw_value) if isinstance(raw_value, str) else None
    privacy_value = details.get("privacy_observation")
    if not isinstance(privacy_value, Mapping):
        return raw
    nudity = privacy_value.get("nudity_level")
    sexual = privacy_value.get("sexual_or_erotic_context")
    intimate = privacy_value.get("intimate_nonsexual_context")
    evidence_value = privacy_value.get("visible_sensitive_evidence")
    has_evidence = isinstance(evidence_value, str) and bool(evidence_value.strip())
    if raw in {None, "none"}:
        if nudity == "explicit":
            return "sexually_explicit"
        if nudity in {"partial", "topless", "nude"}:
            return "nude"
        if nudity == "unknown" or sexual == "uncertain" or intimate == "uncertain":
            return "uncertain"
        if sexual == "yes":
            return "suggestive"
        if intimate == "yes":
            return "intimate"
        if has_evidence:
            return "uncertain"
    elif (
        privacy_value.get("sensitivity_class") is not None
        and nudity in {None, "none"}
        and sexual in {None, "no"}
        and intimate in {None, "no"}
        and not has_evidence
    ):
        return "none"
    return raw
