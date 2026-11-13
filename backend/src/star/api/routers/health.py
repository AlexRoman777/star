from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status

from star.core.config import Settings, get_settings
from star.db.session import probe_database

router = APIRouter(prefix="/health", tags=["Health"])
logger = logging.getLogger(__name__)


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    try:
        await probe_database(settings)
    except Exception as exc:
        logger.warning("health_ready_check_failed", extra={"error_class": type(exc).__name__})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready"}
    return {"status": "ready"}
