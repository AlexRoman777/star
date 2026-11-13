from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Separate from the scheduler lock: the reset endpoint takes both locks, while
# worker API transactions take this one in shared mode. This lets workers run
# concurrently in normal operation but makes reset wait for every in-flight
# lease/result transaction and prevents a new one from entering mid-reset.
RESET_RUNTIME_LOCK_ID = 7_061_720_260_003


async def acquire_reset_runtime_shared(session: AsyncSession) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.scalar(select(func.pg_advisory_xact_lock_shared(RESET_RUNTIME_LOCK_ID)))


async def acquire_reset_runtime_exclusive(session: AsyncSession) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.scalar(select(func.pg_advisory_xact_lock(RESET_RUNTIME_LOCK_ID)))


@asynccontextmanager
async def hold_reset_runtime_shared(session: AsyncSession) -> AsyncIterator[None]:
    """Hold a shared reset barrier for a whole API request.

    This uses a PostgreSQL *session* advisory lock intentionally: several
    bounded actions commit before local Immich network I/O, so a transaction
    lock alone could disappear halfway through one logical request. Shared
    holders remain concurrent with each other; an exclusive reset waits until
    every mutating request has left the critical section.
    """

    if session.get_bind().dialect.name != "postgresql":
        yield
        return
    await session.scalar(select(func.pg_advisory_lock_shared(RESET_RUNTIME_LOCK_ID)))
    await session.commit()
    try:
        yield
    finally:
        # Endpoint code must commit explicitly. Roll back any forgotten or
        # failed transaction before issuing the session-level unlock so no
        # pending ORM state is accidentally flushed by the cleanup query.
        await session.rollback()
        await session.scalar(select(func.pg_advisory_unlock_shared(RESET_RUNTIME_LOCK_ID)))
        await session.rollback()


__all__ = [
    "RESET_RUNTIME_LOCK_ID",
    "acquire_reset_runtime_exclusive",
    "acquire_reset_runtime_shared",
    "hold_reset_runtime_shared",
]
