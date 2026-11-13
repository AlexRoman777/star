from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings, get_settings
from star.db.models import Session, User
from star.db.session import get_session
from star.domain.enums import Role
from star.services.auth import resolve_session


@dataclass(frozen=True)
class Principal:
    user: User
    session: Session


async def get_principal(
    auth_session: str | None = Cookie(default=None, alias="star_session"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    resolved = await resolve_session(session, settings, auth_session)
    if resolved is None:
        # Authentication is a read transaction. Release it before returning an
        # error so malformed or expired cookies cannot occupy a pooled
        # connection until response teardown.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    user, user_session = resolved
    # FastAPI keeps yield dependencies alive for FileResponse and
    # StreamingResponse bodies. End the authentication-only transaction here;
    # expire_on_commit=False keeps the resolved principal usable and any route
    # database work starts a fresh, short transaction on demand.
    await session.commit()
    return Principal(user=user, session=user_session)


def require_roles(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
        return principal

    return dependency


def request_id(request: Request) -> str:
    return str(request.state.request_id)
