from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import User


def _advisory_lock_key(family: str) -> int:
    raw = hashlib.sha256(f"star:{family}".encode()).digest()[:8]
    return int.from_bytes(raw, byteorder="big", signed=True)


async def lock_operation_family(
    session: AsyncSession,
    *,
    family: str,
    actor_id: uuid.UUID | None,
) -> None:
    """Serialize a singleton owner operation for the current transaction.

    PostgreSQL advisory transaction locks remain effective even when no active
    run row exists yet, which closes the classic check-empty-then-insert race.
    SQLite is used only by local tests; locking the owner row preserves the
    intended ordering there without introducing a production-only table.
    """

    if session.get_bind().dialect.name == "postgresql":
        await session.scalar(select(func.pg_advisory_xact_lock(_advisory_lock_key(family))))
        return
    if actor_id is None:
        # SQLite is used only by local single-process tests. PostgreSQL's
        # advisory lock above is the production serialization primitive for
        # machine-owned operations that have no Star user row.
        return
    owner_id = await session.scalar(select(User.id).where(User.id == actor_id).with_for_update())
    if owner_id is None:
        raise ValueError("owner account is unavailable")


__all__ = ["lock_operation_family"]
