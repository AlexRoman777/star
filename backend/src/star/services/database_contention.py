from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def postgres_sqlstate(exc: BaseException) -> str | None:
    """Extract a bounded PostgreSQL SQLSTATE through database wrappers."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and len(value) == 5 and value.isalnum():
                return value
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def is_postgres_deadlock(exc: BaseException) -> bool:
    """Recognize SQLSTATE 40P01 through SQLAlchemy and asyncpg wrappers."""

    return postgres_sqlstate(exc) == "40P01"


def is_postgres_retryable_contention(exc: BaseException) -> bool:
    """Recognize transaction contention that is safe to replay in a fresh session."""

    return postgres_sqlstate(exc) in {"40P01", "40001", "55P03"}


async def retry_postgres_deadlock[ResultT](
    operation: Callable[[], Awaitable[ResultT]],
    *,
    attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ResultT:
    """Retry a whole fresh transaction after PostgreSQL selects it as deadlock victim."""

    if attempts < 1:
        raise ValueError("deadlock retry attempts must be positive")
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as exc:
            if not is_postgres_deadlock(exc) or attempt + 1 >= attempts:
                raise
            await sleep(0.1 * (attempt + 1))
    raise RuntimeError("deadlock retry exhausted without a result")  # pragma: no cover


async def retry_postgres_contention[ResultT](
    operation: Callable[[], Awaitable[ResultT]],
    *,
    attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ResultT:
    """Replay bounded PostgreSQL contention victims with a fresh transaction."""

    if attempts < 1:
        raise ValueError("database contention retry attempts must be positive")
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as exc:
            if not is_postgres_retryable_contention(exc) or attempt + 1 >= attempts:
                raise
            await sleep(0.1 * (attempt + 1))
    raise RuntimeError("database contention retry exhausted without a result")  # pragma: no cover
