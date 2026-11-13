from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from argon2.low_level import Type
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import (
    Session,
    User,
)
from star.domain.enums import Role


@dataclass(frozen=True)
class AuthResult:
    user: User
    session_token: str
    session: Session


_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("star-invalid-password-sentinel")
_MAX_FAILED_PASSWORD_ATTEMPTS = 5
_PASSWORD_LOCKOUT_MINUTES = 15


def _is_expired(value: datetime) -> bool:
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized <= utc_now()


async def has_users(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count()).select_from(User))
    return bool(count)


async def authentication_status(session: AsyncSession) -> tuple[bool, bool]:
    user_count = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    administrator_count = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMINISTRATOR)
        )
        or 0
    )
    passwordless_administrator_exists = bool(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == Role.ADMINISTRATOR,
                User.is_active.is_(True),
                User.password_hash.is_(None),
            )
        )
    )
    initial_password_setup_available = user_count == 0 or passwordless_administrator_exists
    return administrator_count > 0, initial_password_setup_available


async def _hash_password(password: str) -> str:
    return await asyncio.to_thread(_PASSWORD_HASHER.hash, password)


async def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(_PASSWORD_HASHER.verify, password_hash, password)
    except VerifyMismatchError, VerificationError:
        return False


async def _consume_invalid_password_work(password: str) -> None:
    await _verify_password(_DUMMY_PASSWORD_HASH, password)


async def bootstrap_password_setup(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    display_name: str | None,
    password: str,
) -> AuthResult:
    user = await session.scalar(select(User).where(User.username == username).with_for_update())
    users_exist = await has_users(session)
    if user is None and not users_exist:
        if not display_name:
            raise ValueError("display name is required for first setup")
        user = User(
            username=username,
            display_name=display_name,
            role=Role.ADMINISTRATOR,
            is_active=True,
        )
        session.add(user)
        await session.flush()
    elif (
        user is None
        or not user.is_active
        or user.role != Role.ADMINISTRATOR
        or user.password_hash is not None
    ):
        raise ValueError("initial password setup is not available")

    user.password_hash = await _hash_password(password)
    user.failed_password_attempts = 0
    user.password_locked_until = None
    return await create_session(session, settings, user)


async def password_login(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
) -> AuthResult:
    user = await session.scalar(
        select(User).where(User.username == username, User.is_active.is_(True)).with_for_update()
    )
    if user is None:
        await _consume_invalid_password_work(password)
        raise ValueError("invalid username or password")
    if user.password_locked_until is not None and not _is_expired(user.password_locked_until):
        await _consume_invalid_password_work(password)
        raise ValueError("invalid username or password")
    if user.password_hash is None:
        await _consume_invalid_password_work(password)
        raise ValueError("invalid username or password")

    valid = await _verify_password(user.password_hash, password)
    if not valid:
        user.failed_password_attempts += 1
        if user.failed_password_attempts >= _MAX_FAILED_PASSWORD_ATTEMPTS:
            user.password_locked_until = utc_now() + timedelta(minutes=_PASSWORD_LOCKOUT_MINUTES)
        raise ValueError("invalid username or password")

    if _PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = await _hash_password(password)
    user.failed_password_attempts = 0
    user.password_locked_until = None
    user.last_password_login_at = utc_now()
    return await create_session(session, settings, user)


async def create_session(
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> AuthResult:
    token = secrets.token_urlsafe(48)
    model = Session(
        user_id=user.id,
        token_hash=hash_session_token(token, settings.require_session_secret()),
        expires_at=utc_now() + timedelta(hours=settings.session_ttl_hours),
        reauthenticated_at=utc_now(),
    )
    session.add(model)
    await session.flush()
    return AuthResult(user=user, session_token=token, session=model)


async def resolve_session(
    session: AsyncSession,
    settings: Settings,
    token: str | None,
) -> tuple[User, Session] | None:
    if not token:
        return None
    token_hash = hash_session_token(token, settings.require_session_secret())
    auth_session = await session.scalar(
        select(Session).where(
            Session.token_hash == token_hash,
            Session.revoked_at.is_(None),
        )
    )
    if auth_session is None or _is_expired(auth_session.expires_at):
        return None
    user = await session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        return None
    return user, auth_session
