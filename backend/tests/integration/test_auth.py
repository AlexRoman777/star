from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import get_settings
from star.db.base import utc_now
from star.db.models import Session, User
from star.db.session import get_session
from star.main import app


async def test_first_administrator_can_be_created_from_trusted_lan_without_token(
    session_factory,
    test_settings,
) -> None:
    test_settings.local_password_setup_enabled = True

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as database_session:
            yield database_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            status = await client.get("/api/v1/auth/status")
            assert status.status_code == 200
            assert status.json() == {
                "administrator_exists": False,
                "initial_password_setup_available": True,
                "password_login_enabled": True,
                "local_password_setup_available": True,
            }

            setup = await client.post(
                "/api/v1/auth/password/setup",
                headers={
                    "X-Star-Edge": "star-edge",
                    "X-Star-Client-IP": "192.168.1.20",
                },
                json={
                    "username": "owner",
                    "display_name": "Owner",
                    "password": "eight888",
                },
            )
            assert setup.status_code == 200, setup.text
            assert setup.json()["user"]["username"] == "owner"
            assert "star_session=" in setup.headers["set-cookie"]
    finally:
        app.dependency_overrides.clear()


async def test_browser_session_expires_after_thirty_days(
    session_factory,
    test_settings,
) -> None:
    test_settings.local_password_setup_enabled = True
    test_settings.session_ttl_hours = 24 * 30

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as database_session:
            yield database_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            setup = await client.post(
                "/api/v1/auth/password/setup",
                headers={
                    "X-Star-Edge": "star-edge",
                    "X-Star-Client-IP": "192.168.1.20",
                },
                json={
                    "username": "owner",
                    "display_name": "Owner",
                    "password": "eight888",
                },
            )
        assert setup.status_code == 200, setup.text
        assert "Max-Age=2592000" in setup.headers["set-cookie"]

        async with session_factory() as session:
            auth_session = await session.scalar(select(Session))
            assert auth_session is not None
            expires_at = auth_session.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            remaining = expires_at - utc_now()
            assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)
    finally:
        app.dependency_overrides.clear()


async def test_password_login_is_generic_and_locks_after_repeated_failures(
    authenticated_context,
    session_factory,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as setup_client:
        setup = await setup_client.post(
            "/api/v1/auth/password/setup",
            headers={"X-Star-Bootstrap-Token": "unit-test-bootstrap-token"},
            json={
                "username": "owner",
                "display_name": None,
                "password": "a sufficiently long local password",
            },
        )
        assert setup.status_code == 200

    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        unknown = await client.post(
            "/api/v1/auth/password/login",
            json={"username": "unknown", "password": "an incorrect password"},
        )
        assert unknown.status_code == 401
        assert unknown.json()["detail"] == "invalid username or password"

        for _ in range(5):
            failed = await client.post(
                "/api/v1/auth/password/login",
                json={"username": "owner", "password": "an incorrect password"},
            )
            assert failed.status_code == 401
            assert failed.json()["detail"] == "invalid username or password"

        locked = await client.post(
            "/api/v1/auth/password/login",
            json={"username": "owner", "password": "a sufficiently long local password"},
        )
        assert locked.status_code == 401
        assert locked.json()["detail"] == "invalid username or password"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "owner"))
        assert user is not None
        assert user.failed_password_attempts == 5
        assert user.password_locked_until is not None
