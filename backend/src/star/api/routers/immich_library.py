from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.models import ImmichConnection
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    ImmichLibraryOverviewResponse,
)
from star.immich.client import ImmichApiError
from star.services.immich_library import (
    get_library_overview,
)

router = APIRouter(prefix="/immich", tags=["Immich library"])
admin = require_roles(Role.ADMINISTRATOR)
logger = logging.getLogger(__name__)


async def _connection_or_404(
    session: AsyncSession,
    connection_id: uuid.UUID,
) -> ImmichConnection:
    connection = await session.get(ImmichConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    return connection


@router.get(
    "/connections/{connection_id}/library-overview",
    response_model=ImmichLibraryOverviewResponse,
)
async def library_overview(
    connection_id: uuid.UUID,
    _principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ImmichLibraryOverviewResponse:
    connection = await _connection_or_404(session, connection_id)
    try:
        return await get_library_overview(
            session,
            connection=connection,
            cipher=SecretCipher(settings.require_encryption_key()),
        )
    except (ImmichApiError, ValueError) as exc:
        logger.exception(
            "immich library overview failed",
            extra={
                "error_class": type(exc).__name__,
                "connection_id": str(connection_id),
                "operation": "library_overview",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich library overview is temporarily unavailable",
        ) from exc
