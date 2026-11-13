"""Owner-facing model slots per machine (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.core.config import Settings, get_settings
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import ModelSlotsResponse, QueueOverviewResponse
from star.services.model_slots import model_slots_summary
from star.services.queue_overview import queue_overview

router = APIRouter(prefix="/workers", tags=["Workers"])


@router.get("/model-slots", response_model=ModelSlotsResponse)
async def get_model_slots(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ModelSlotsResponse:
    """Who holds each machine's exclusive lane, since when, and what takes over next.

    Pure read: no lease, affinity, or queue row changes and nothing is committed.
    """

    return await model_slots_summary(session, settings)


@router.get("/queues", response_model=QueueOverviewResponse)
async def get_queue_overview(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QueueOverviewResponse:
    """Queue lengths per model and route, and which machines share each queue.

    Every queue is global: a lane names the machines currently able to take
    from it, and ``shared`` says two or more do. Pure read.
    """

    return await queue_overview(session, settings)
