from __future__ import annotations

from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, get_principal
from star.core.config import Settings, get_settings
from star.core.crypto import constant_time_equal
from star.db.base import utc_now
from star.db.session import get_session
from star.domain.schemas import (
    AuthenticationStatusResponse,
    InitialPasswordSetupRequest,
    PasswordLoginRequest,
    SessionResponse,
    UserSummary,
)
from star.services.auth import (
    authentication_status,
    bootstrap_password_setup,
    password_login,
)
from star.services.network_trust import trusted_private_address

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    expires_at = utc_now() + timedelta(hours=settings.session_ttl_hours)
    response.set_cookie(
        "star_session",
        token,
        max_age=settings.session_ttl_hours * 3600,
        expires=expires_at,
        secure=settings.public_origin.startswith("https://"),
        httponly=True,
        samesite="strict",
        path="/",
    )


def _require_recent_sign_in(principal: Principal) -> None:
    reauthenticated_at = principal.session.reauthenticated_at
    if reauthenticated_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a recent sign-in is required",
        )
    normalized = (
        reauthenticated_at if reauthenticated_at.tzinfo else reauthenticated_at.replace(tzinfo=UTC)
    )
    if normalized < utc_now() - timedelta(minutes=15):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a recent sign-in is required",
        )


@router.get("/status", response_model=AuthenticationStatusResponse)
async def auth_status(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticationStatusResponse:
    administrator_exists, initial_password_setup_available = await authentication_status(session)
    return AuthenticationStatusResponse(
        administrator_exists=administrator_exists,
        initial_password_setup_available=initial_password_setup_available,
        local_password_setup_available=(
            initial_password_setup_available and settings.local_password_setup_enabled
        ),
    )


@router.post("/password/setup", response_model=SessionResponse)
async def initial_password_setup(
    payload: InitialPasswordSetupRequest,
    response: Response,
    request: Request,
    x_star_bootstrap_token: str | None = Header(default=None),
    x_star_edge: str | None = Header(default=None, alias="X-Star-Edge"),
    x_star_client_ip: str | None = Header(default=None, alias="X-Star-Client-IP"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    expected = settings.get_bootstrap_token()
    bootstrap_valid = bool(
        expected
        and x_star_bootstrap_token
        and constant_time_equal(expected, x_star_bootstrap_token)
    )
    local_setup_valid = False
    if settings.local_password_setup_enabled:
        try:
            trusted_private_address(
                client_host=request.client.host if request.client else None,
                edge_marker=x_star_edge,
                forwarded_client_ip=x_star_client_ip,
                require_edge=settings.worker_auto_enroll_require_edge,
                allowed_cidrs=settings.worker_auto_enroll_cidrs,
            )
            local_setup_valid = True
        except ValueError:
            local_setup_valid = False
    if not bootstrap_valid and not local_setup_valid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid bootstrap token")
    try:
        result = await bootstrap_password_setup(
            session,
            settings,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    _set_session_cookie(response, result.session_token, settings)
    return SessionResponse(authenticated=True, user=UserSummary.model_validate(result.user))


@router.post("/password/login", response_model=SessionResponse)
async def local_password_login(
    payload: PasswordLoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    try:
        result = await password_login(
            session,
            settings,
            username=payload.username,
            password=payload.password,
        )
    except ValueError as exc:
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        ) from exc
    await session.commit()
    _set_session_cookie(response, result.session_token, settings)
    return SessionResponse(authenticated=True, user=UserSummary.model_validate(result.user))


@router.get("/session", response_model=SessionResponse)
async def current_session(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    expires_at = principal.session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    renewal_window = timedelta(hours=settings.session_ttl_hours / 2)
    if expires_at <= utc_now() + renewal_window:
        principal.session.expires_at = utc_now() + timedelta(hours=settings.session_ttl_hours)
        await session.commit()
        token = request.cookies.get("star_session")
        if token:
            _set_session_cookie(response, token, settings)
    return SessionResponse(authenticated=True, user=UserSummary.model_validate(principal.user))
