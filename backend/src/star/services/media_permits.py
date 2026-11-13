"""Expiring coordinator-wide upstream permits; cache hits do not use these slots."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from weakref import WeakKeyDictionary

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from star.db.base import utc_now
from star.db.models.media_permits import MediaPermit

_factories: WeakKeyDictionary[AsyncEngine, async_sessionmaker[AsyncSession]] = WeakKeyDictionary()


def permit_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    factory = _factories.get(engine)
    if factory is None:
        # One reserved connection per API process avoids waiting on a pool whose
        # request sessions are themselves waiting for an upstream permit.
        bind = (
            create_async_engine(
                engine.url, pool_size=1, max_overflow=0, pool_timeout=3, pool_pre_ping=True
            )
            if engine.dialect.name == "postgresql"
            else engine
        )
        factory = async_sessionmaker(bind, expire_on_commit=False)
        _factories[engine] = factory
    return factory


async def close_permit_pools() -> None:
    for original, factory in list(_factories.items()):
        bind = factory.kw["bind"]
        if bind is not original:
            await bind.dispose()
    _factories.clear()


class MediaPermitUnavailable(RuntimeError):
    pass


async def acquire_permit(
    factory: async_sessionmaker[AsyncSession], *, background: bool = False
) -> tuple[int, uuid.UUID]:
    owner = uuid.uuid4()
    async with factory() as session:
        insert = (
            pg_insert(MediaPermit)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(MediaPermit)
        )
        await session.execute(
            insert.values([{"slot": slot} for slot in range(1, 7)]).on_conflict_do_nothing(
                index_elements=["slot"]
            )
        )
        await session.commit()
    for attempt in range(3):
        async with factory() as session:
            now = utc_now()
            statement = select(MediaPermit).where(
                or_(MediaPermit.owner.is_(None), MediaPermit.expires_at <= now)
            )
            if background:
                statement = statement.where(MediaPermit.slot <= 2)
            permit = await session.scalar(
                statement.order_by(MediaPermit.slot.desc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if permit is not None:
                # The predicate also makes the SQLite/non-locking path compare-and-set.
                claimed = await session.scalar(
                    update(MediaPermit)
                    .where(
                        MediaPermit.slot == permit.slot,
                        or_(MediaPermit.owner.is_(None), MediaPermit.expires_at <= now),
                    )
                    .values(owner=owner, expires_at=now + timedelta(seconds=90))
                    .execution_options(synchronize_session=False)
                    .returning(MediaPermit.slot)
                )
                await session.commit()
                if claimed is not None:
                    return claimed, owner
        if attempt < 2:
            await asyncio.sleep(0.1 * (attempt + 1))
    raise MediaPermitUnavailable("upstream concurrency budget is busy")


async def release_permit(
    factory: async_sessionmaker[AsyncSession], slot: int, owner: uuid.UUID
) -> None:
    async with factory() as session:
        await session.execute(
            update(MediaPermit)
            .where(MediaPermit.slot == slot, MediaPermit.owner == owner)
            .values(owner=None, expires_at=None)
        )
        await session.commit()


async def guarded_media_call[T](
    factory: async_sessionmaker[AsyncSession],
    operation: Callable[[], Awaitable[T]],
    *,
    background: bool = False,
) -> T:
    slot, owner = await acquire_permit(factory, background=background)

    async def renew() -> None:
        while True:
            await asyncio.sleep(30)
            async with factory() as session:
                found = await session.scalar(
                    update(MediaPermit)
                    .where(
                        MediaPermit.slot == slot,
                        MediaPermit.owner == owner,
                    )
                    .values(expires_at=utc_now() + timedelta(seconds=90))
                    .returning(MediaPermit.slot)
                )
                await session.commit()
                if found is None:
                    raise MediaPermitUnavailable("upstream permit was superseded")

    async def perform() -> T:
        return await operation()

    fetch = asyncio.create_task(perform())
    renewal = asyncio.create_task(renew())
    try:
        completed, _ = await asyncio.wait({fetch, renewal}, return_when=asyncio.FIRST_COMPLETED)
        if renewal in completed:
            await renewal
        return await fetch
    finally:
        for task in (fetch, renewal):
            task.cancel()
        for task in (fetch, renewal):
            with suppress(asyncio.CancelledError, Exception):
                await task
        # Expiry recovers the slot if the database itself is unavailable here.
        with suppress(Exception):
            await release_permit(factory, slot, owner)
