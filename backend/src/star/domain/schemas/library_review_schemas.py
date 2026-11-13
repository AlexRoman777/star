"""What a nightly library review is allowed to say.

The pass reads counts and writes conclusions. Its output is deliberately narrow:
a short summary, a handful of findings that each cite the number they came from,
and bounded proposals drawn from a fixed set of actions. A free-form answer would
be impossible to check and impossible to act on safely, and model prose must
never become an instruction on its own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema

LIBRARY_REVIEW_MAX_FINDINGS = 6
LIBRARY_REVIEW_MAX_PROPOSALS = 4

# The only things a review may ask for. Each is something the owner can accept
# with one action and each is reversible; nothing here edits a prompt directly,
# because a prompt change has to be measured against the owner's own ratings
# before it can be trusted.
LIBRARY_REVIEW_ACTIONS = (
    # Re-review a tier or the photographs judged by an older prompt.
    "review_again",
    # Put the four-star tier through more comparisons so the top settles.
    "compare_more",
    # Draft a prompt change for the benchmark to judge, never to apply.
    "draft_prompt_change",
    # Say plainly that the right move is to wait.
    "wait",
)

LIBRARY_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings", "proposals"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1_200},
        "findings": {
            "type": "array",
            "minItems": 0,
            "maxItems": LIBRARY_REVIEW_MAX_FINDINGS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["observation", "evidence", "confidence"],
                "properties": {
                    "observation": {"type": "string", "minLength": 1, "maxLength": 400},
                    # Which number in the evidence pack this came from, so a
                    # claim can be checked rather than believed.
                    "evidence": {"type": "string", "minLength": 1, "maxLength": 240},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        },
        "proposals": {
            "type": "array",
            "minItems": 0,
            "maxItems": LIBRARY_REVIEW_MAX_PROPOSALS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "reason", "detail"],
                "properties": {
                    "action": {"type": "string", "enum": list(LIBRARY_REVIEW_ACTIONS)},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    "detail": {"type": "string", "minLength": 1, "maxLength": 400},
                },
            },
        },
    },
}


class LibraryReviewSubmission(ApiSchema):
    """One completed nightly reading, as the worker returns it."""

    note_id: uuid.UUID
    model_key: str = Field(max_length=256)
    # Validated against LIBRARY_REVIEW_SCHEMA by the model runtime; the
    # coordinator keeps only the fields the schema allows.
    value: dict[str, Any] = Field(default_factory=dict)
    inference_runs: list[dict[str, Any]] = Field(default_factory=list, max_length=8)


# What the owner may do with a proposal. "accept" is deliberately narrow: it
# starts the one bounded, reversible piece of work the action names, and never
# edits a prompt or a rating.
LIBRARY_REVIEW_DECISIONS = ("accepted", "dismissed")


class LibraryReviewProposalView(ApiSchema):
    """One suggestion from a nightly reading, with what became of it."""

    index: int
    action: str
    reason: str
    detail: str
    decision: str | None = None
    decided_at: datetime | None = None
    outcome: str | None = None


class LibraryReviewFindingView(ApiSchema):
    observation: str
    evidence: str
    confidence: str


class LibraryReviewNoteView(ApiSchema):
    """One night in the notebook, as the owner reads it."""

    id: uuid.UUID
    state: str
    model_key: str
    summary: str
    created_at: datetime | None = None
    completed_at: datetime | None = None
    failure_class: str | None = None
    findings: list[LibraryReviewFindingView] = Field(default_factory=list)
    proposals: list[LibraryReviewProposalView] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class LibraryReviewDecisionRequest(ApiSchema):
    decision: Literal["accepted", "dismissed"]


class LibraryReviewDecisionResponse(ApiSchema):
    note: LibraryReviewNoteView
    # Said plainly, because "accepted" alone would not tell the owner whether
    # anything actually started.
    outcome: str


__all__ = [
    "LIBRARY_REVIEW_ACTIONS",
    "LIBRARY_REVIEW_DECISIONS",
    "LIBRARY_REVIEW_MAX_FINDINGS",
    "LIBRARY_REVIEW_MAX_PROPOSALS",
    "LIBRARY_REVIEW_SCHEMA",
    "LibraryReviewDecisionRequest",
    "LibraryReviewDecisionResponse",
    "LibraryReviewFindingView",
    "LibraryReviewNoteView",
    "LibraryReviewProposalView",
    "LibraryReviewSubmission",
]
