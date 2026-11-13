from __future__ import annotations

import logging

from sqlalchemy import text

from star.db.session import engine

logger = logging.getLogger(__name__)


async def ensure_writeback_batches_schema() -> None:
    try:
        async with engine.begin() as conn:
            has_blocked_reasons = await conn.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'writeback_batches'
                          AND column_name = 'blocked_reasons'
                    )
                    """,
                )
            )

            if not has_blocked_reasons:
                logger.warning(
                    "writeback_batches.blocked_reasons missing; applying compatibility migration",
                )
                await conn.execute(
                    text(
                        """
                        ALTER TABLE writeback_batches
                        ADD COLUMN IF NOT EXISTS blocked_reasons JSONB NOT NULL DEFAULT '{}'::jsonb
                        """,
                    )
                )
                logger.info("Added writeback_batches.blocked_reasons column")
    except Exception as exc:
        logger.exception(
            "failed to verify/repair writeback batch schema compatibility",
            extra={"error_class": type(exc).__name__},
        )
        raise
