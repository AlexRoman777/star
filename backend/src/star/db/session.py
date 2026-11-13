from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from star.core.config import Settings, get_settings
from star.services.action_safety import STATE_CHANGING_GET_ENDPOINTS
from star.services.reset_barrier import hold_reset_runtime_shared


def create_engine(settings: Settings) -> AsyncEngine:
    if settings.async_database_url.startswith("postgresql+"):
        return create_async_engine(
            settings.async_database_url,
            pool_pre_ping=True,
            echo=False,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            pool_recycle=settings.database_pool_recycle_seconds,
            pool_use_lifo=True,
            connect_args={
                "server_settings": {
                    "application_name": f"star-{settings.coordinator_role}"[:63],
                    # A forgotten read transaction must not pin one of the
                    # coordinator's bounded connections indefinitely. Ordinary route
                    # work commits or rolls back well inside this guard.
                    "idle_in_transaction_session_timeout": "30000",
                }
            },
        )
    return create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        echo=False,
    )


async def probe_database(settings: Settings) -> None:
    """Probe PostgreSQL without competing for the request connection pool.

    Readiness must distinguish a dead database from a temporarily saturated
    API pool. A short-lived NullPool connection provides that reserved control
    path and is disposed after every low-frequency container health check.
    """

    connect_args: dict[str, object] = {}
    if settings.async_database_url.startswith("postgresql+"):
        connect_args = {
            "server_settings": {
                "application_name": "star-readiness",
                "idle_in_transaction_session_timeout": "5000",
            },
            "timeout": min(5.0, settings.database_pool_timeout_seconds),
        }
    readiness_engine = create_async_engine(
        settings.async_database_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    try:
        async with readiness_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await readiness_engine.dispose()


settings = get_settings()
engine = create_engine(settings)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def reset_safe_session(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Create one pinned mutation session outside an exclusive reset.

    A PostgreSQL session advisory lock belongs to one physical connection. The
    explicit connection scope is essential: an ordinary Session.commit may
    otherwise return that connection to the pool halfway through the logical
    operation, leaking the lock and losing reset exclusion.
    """

    selected_factory = factory or session_factory
    bind = selected_factory.kw.get("bind")
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as connection:
            pinned_factory = async_sessionmaker(connection, expire_on_commit=False)
            async with pinned_factory() as session, hold_reset_runtime_shared(session):
                yield session
        return
    async with selected_factory() as session, hold_reset_runtime_shared(session):
        yield session


def _request_requires_reset_barrier(request: Request) -> bool:
    route = request.scope.get("route")
    endpoint_name = getattr(route, "name", None)
    mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"} or (
        request.method == "GET" and endpoint_name in STATE_CHANGING_GET_ENDPOINTS
    )
    reset_execute = request.url.path.endswith("/library-reset/execute")
    return mutating and not reset_execute


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    if _request_requires_reset_barrier(request):
        async with reset_safe_session() as session:
            yield session
        return
    async with session_factory() as session:
        yield session
