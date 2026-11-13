from __future__ import annotations

import asyncio
import contextlib
import logging

from star.core.config import get_settings
from star.core.crypto import SecretCipher
from star.core.logging import configure_logging
from star.db.compat import ensure_writeback_batches_schema
from star.db.session import reset_safe_session
from star.services.service_heartbeats import run_service_heartbeat
from star.services.writeback import execute_next_approved_batch

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await ensure_writeback_batches_schema()
    if not settings.writeback_enabled:
        raise SystemExit("write-back is disabled; refusing to start isolated writer")
    cipher = SecretCipher(settings.require_encryption_key())
    logger.info("isolated rating write-back worker started")
    heartbeat_task = asyncio.create_task(
        run_service_heartbeat(
            service_key="writeback",
            settings=settings,
            role="isolated-writer",
        ),
        name="writeback-service-heartbeat",
    )
    try:
        while True:
            async with reset_safe_session() as session:
                found = await execute_next_approved_batch(session, cipher=cipher)
            await asyncio.sleep(2 if not found else 0.5)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


if __name__ == "__main__":
    asyncio.run(run())
