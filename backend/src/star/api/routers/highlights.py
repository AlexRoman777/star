from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import ApiSchema
from star.services.highlight_league import (
    highlight_overview,
    reconcile_requested_highlight_pass,
    request_highlight_filter,
    request_highlight_pass,
)
from star.services.library_program import get_or_create_library_program

router = APIRouter(prefix="/highlights", tags=["highlights"])
owner = require_roles(Role.ADMINISTRATOR)


class HighlightBackfillRequest(ApiSchema):
    limit: int = Field(default=1000, ge=1, le=1000)
    pass_name: Literal["four_to_five", "filter_highlights"] | None = None


@router.get("/overview")
async def get_highlight_overview(
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    return await highlight_overview(session)


@router.post("/backfill")
async def start_highlight_backfill(
    payload: HighlightBackfillRequest,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    program = await get_or_create_library_program(session, actor_id=principal.user.id)
    phase_name = payload.pass_name or "four_to_five"
    try:
        requested = (
            await request_highlight_filter(session, program=program)
            if phase_name == "filter_highlights"
            else await request_highlight_pass(
                session,
                program=program,
                phase_name=phase_name,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    changed = await reconcile_requested_highlight_pass(
        session,
        program=program,
        candidate_limit=payload.limit,
    )
    await session.commit()
    return {
        "changed": changed,
        "requested": requested,
        "chunk_limit": payload.limit,
        "pass_name": phase_name,
        "durable": True,
    }
