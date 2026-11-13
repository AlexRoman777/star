from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.db.base import utc_now
from star.db.models import PerformanceSnapshot
from star.db.session import get_session
from star.domain.enums import Role

router = APIRouter(prefix="/performance", tags=["Performance"])


@router.get("")
async def performance_snapshot(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    snapshot = await session.get(PerformanceSnapshot, "fleet")
    if snapshot is None:
        return {"available": False, "stale": False, "workers": {}, "forecasts": {}}
    observed = (
        snapshot.observed_at.replace(tzinfo=UTC)
        if snapshot.observed_at.tzinfo is None
        else snapshot.observed_at
    )
    return {
        **snapshot.payload,
        "available": True,
        "stale": (utc_now() - observed).total_seconds() > 90,
    }
