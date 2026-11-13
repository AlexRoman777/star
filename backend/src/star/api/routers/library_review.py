"""The notebook the library keeps about itself.

Once a night the strongest model available reads a pack of counts and writes
what it concludes. These routes are how the owner reads that notebook and
decides what, if anything, should come of it. Accepting a proposal starts one
bounded, reversible piece of work; nothing here is ever applied on its own.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.db.models import LibraryReviewNote
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas.library_review_schemas import (
    LibraryReviewDecisionRequest,
    LibraryReviewDecisionResponse,
    LibraryReviewNoteView,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_review import decide_proposal, note_view

router = APIRouter(prefix="/library-review", tags=["Library review"])
owner = require_roles(Role.ADMINISTRATOR)

# A handful of nights is what anyone reads. The rest stays in the table.
NOTE_PAGE_SIZE = 20


@router.get("/notes", response_model=list[LibraryReviewNoteView])
async def list_library_review_notes(
    limit: int = NOTE_PAGE_SIZE,
    _: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[LibraryReviewNoteView]:
    bounded = max(1, min(limit, 100))
    notes = list(
        await session.scalars(
            select(LibraryReviewNote).order_by(LibraryReviewNote.created_at.desc()).limit(bounded)
        )
    )
    return [LibraryReviewNoteView(**note_view(note)) for note in notes]


@router.post(
    "/notes/{note_id}/proposals/{index}",
    response_model=LibraryReviewDecisionResponse,
    status_code=status.HTTP_200_OK,
)
async def decide_library_review_proposal(
    note_id: uuid.UUID,
    index: int,
    payload: LibraryReviewDecisionRequest,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> LibraryReviewDecisionResponse:
    note = await session.get(LibraryReviewNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="no such review note")
    try:
        outcome = await decide_proposal(
            session,
            note=note,
            index=index,
            decision=payload.decision,
            actor_id=principal.user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library.review.proposal.decided",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"note_id": str(note.id), "proposal_index": index},
            correlation_id=correlation_id,
            after={
                "decision": payload.decision,
                "outcome": outcome,
                "immich_unchanged": True,
            },
        ),
    )
    await session.commit()
    await session.refresh(note)
    return LibraryReviewDecisionResponse(
        note=LibraryReviewNoteView(**note_view(note)),
        outcome=outcome,
    )
