from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import ServiceHeartbeat
from star.db.session import reset_safe_session

logger = logging.getLogger(__name__)


async def touch_service_heartbeat(
    session: AsyncSession,
    *,
    service_key: str,
    settings: Settings,
    role: str,
    status: str = "online",
    details: dict[str, Any] | None = None,
) -> ServiceHeartbeat:
    heartbeat = await session.scalar(
        select(ServiceHeartbeat).where(ServiceHeartbeat.service_key == service_key)
    )
    if heartbeat is None:
        heartbeat = ServiceHeartbeat(
            service_key=service_key,
            hostname_label=settings.coordinator_name,
            role=role,
            status=status,
            software_version=__version__,
            details=details or {},
        )
        session.add(heartbeat)
    else:
        heartbeat.hostname_label = settings.coordinator_name
        heartbeat.role = role
        heartbeat.status = status
        heartbeat.software_version = __version__
        heartbeat.details = details or {}
        heartbeat.last_heartbeat = utc_now()
    await session.flush()
    return heartbeat


async def run_service_heartbeat(
    *,
    service_key: str,
    settings: Settings,
    role: str,
    details_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    while True:
        try:
            details = await details_provider() if details_provider else {}
            async with reset_safe_session() as session:
                await touch_service_heartbeat(
                    session,
                    service_key=service_key,
                    settings=settings,
                    role=role,
                    details=details,
                )
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "service heartbeat failed",
                extra={
                    "service_key": service_key,
                    "error_class": type(exc).__name__,
                    "coordinator": settings.coordinator_name,
                },
            )
        await asyncio.sleep(settings.service_heartbeat_interval_seconds)
