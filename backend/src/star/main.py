from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from star import __version__
from star.analysis.ranking import AssetProjectionBusyError
from star.api.router import api_router
from star.core.config import get_settings
from star.core.logging import configure_logging
from star.db.compat import ensure_writeback_batches_schema
from star.services.api_diagnostics import record_api_error, record_api_latency
from star.services.database_contention import is_postgres_deadlock
from star.services.pipeline_contract import validate_pipeline_contract

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    validate_pipeline_contract()
    settings.model_root.mkdir(parents=True, exist_ok=True)
    await ensure_writeback_batches_schema()
    try:
        yield
    finally:
        from star.services.media_permits import close_permit_pools

        await close_permit_pools()


app = FastAPI(
    title="Star API",
    version=__version__,
    docs_url="/api/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "If-Match",
        "X-CSRF-Token",
        "X-Star-Bootstrap-Token",
        "X-Star-Display-Name",
        "X-Request-ID",
    ],
    expose_headers=["X-Request-ID", "Server-Timing"],
)


def _request_log_context(
    request: Request,
    started: float,
) -> dict[str, object]:
    route = request.scope.get("route")
    return {
        "request_id": getattr(request.state, "request_id", "n/a"),
        "http_method": request.method,
        "route": getattr(route, "path", "unmatched"),
        "duration_ms": round(
            (time.monotonic() - getattr(request.state, "request_started_at", started)) * 1000, 1
        ),
    }


def _is_routine_worker_poll(
    *,
    method: str,
    route_path: str,
    status_code: int,
) -> bool:
    if method != "POST" or status_code >= 400:
        return False
    route_path = route_path.removeprefix("/api/v1")
    if route_path == "/workers/tasks/lease":
        # Successful leases remain visible. Healthy 204 idleness is already counted
        # already and should not dominate NAS JSON logs.
        return status_code == 204
    return route_path in {
        "/workers/heartbeat",
        "/workers/tasks/{task_id}/heartbeat",
    }


@app.middleware("http")
async def request_context(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.monotonic()
    request.state.request_started_at = started
    quiet_route = request.url.path in {
        "/api/v1/health/live",
        "/api/v1/health/ready",
    } or request.url.path.endswith("/stream")
    try:
        response = await call_next(request)
    except Exception as exc:
        # The exception handler below emits one sanitized warning for known
        # retryable contention. Avoid duplicating it as an ERROR here.
        if not _is_retryable_request_contention(exc):
            logger.exception(
                "api_request_failed_to_dispatch",
                extra={
                    **_request_log_context(request, started),
                    "error_class": type(exc).__name__,
                },
            )
        raise
    route = request.scope.get("route")
    route_path = getattr(route, "path", "unmatched")
    duration_ms = (time.monotonic() - started) * 1000
    response.headers["x-request-id"] = request_id
    response.headers["server-timing"] = f"app;dur={duration_ms:.1f}"
    # What each route costs, kept in this process and read through the
    # diagnostics. "The app is slow" then names a route instead of a feeling.
    if not quiet_route:
        record_api_latency(
            method=request.method,
            route=route_path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    if not quiet_route and not _is_routine_worker_poll(
        method=request.method,
        route_path=route_path,
        status_code=response.status_code,
    ):
        logger.info(
            (
                "api_action_completed"
                if request.method not in {"GET", "HEAD", "OPTIONS"}
                else "api_request_completed"
            ),
            extra={
                **_request_log_context(request, started),
                "status_code": response.status_code,
            },
        )
    return response


app.include_router(api_router)


def _is_postgres_deadlock(exc: BaseException) -> bool:
    return is_postgres_deadlock(exc)


def _is_retryable_request_contention(exc: BaseException) -> bool:
    return (
        isinstance(exc, (AssetProjectionBusyError, SQLAlchemyTimeoutError))
        or (isinstance(exc, DBAPIError) and exc.connection_invalidated)
        or _is_postgres_deadlock(exc)
    )


def _record_request_error(context: dict[str, object]) -> None:
    raw_status = context.get("status_code", 500)
    status_code = raw_status if isinstance(raw_status, int) else 500
    record_api_error(
        method=str(context.get("http_method", "unknown")),
        route=str(context.get("route", "unmatched")),
        status_code=status_code,
        error_class=str(context.get("error_class", "UnknownError")),
        request_id=str(context.get("request_id", "n/a")),
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> Response:
    context = _request_log_context(request, time.monotonic())
    if (
        exc.status_code == 503
        and (exc.headers or {}).get("X-Star-Media-Error") == "immich_preview_busy"
    ):
        logger.info("media_capacity_deferred", extra={**context, "status_code": 503})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Preview capacity is busy; retry shortly",
                "request_id": context["request_id"],
                "error_class": "MediaBackpressure",
            },
            headers={**(exc.headers or {}), "Retry-After": "1"},
        )
    context.update(
        {
            "status_code": exc.status_code,
            "error_class": "HTTPException",
        },
    )
    if exc.status_code == 204:
        context.pop("error_class", None)
        if not _is_routine_worker_poll(
            method=request.method, route_path=str(context["route"]), status_code=204
        ):
            logger.info("api_no_content", extra=context)
        return Response(status_code=204, headers=exc.headers)
    if 400 <= exc.status_code < 500:
        logger.warning("api_http_exception", extra=context)
    else:
        logger.error("api_http_exception", extra=context, exc_info=True)
    _record_request_error(context)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "request_id": context["request_id"],
            "error_class": "HTTPException",
        },
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def _unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, SQLAlchemyTimeoutError):
        context = _request_log_context(request, time.monotonic())
        context.update(
            {
                "status_code": 503,
                "error_class": "DatabasePoolBusyRetryable",
            },
        )
        logger.warning("api_database_pool_busy_retryable", extra=context)
        _record_request_error(context)
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Star database connections are busy; the refresh can retry "
                    "safely and queued work remains durable"
                ),
                "request_id": context["request_id"],
                "error_class": "DatabasePoolBusyRetryable",
            },
            headers={"Retry-After": "2"},
        )
    if isinstance(exc, AssetProjectionBusyError):
        context = _request_log_context(request, time.monotonic())
        context.update(
            {
                "status_code": 503,
                "error_class": "AssetProjectionBusyRetryable",
            },
        )
        logger.warning("api_asset_projection_busy_retryable", extra=context)
        _record_request_error(context)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "temporary asset projection contention; retry the request",
                "request_id": context["request_id"],
                "error_class": "AssetProjectionBusyRetryable",
            },
            headers={"Retry-After": "1"},
        )
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        context = _request_log_context(request, time.monotonic())
        context.update(
            {
                "status_code": 503,
                "error_class": "DatabaseConnectionRetryable",
            },
        )
        logger.warning("api_database_connection_retryable", extra=context)
        _record_request_error(context)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "the database connection changed; retry the request safely",
                "request_id": context["request_id"],
                "error_class": "DatabaseConnectionRetryable",
            },
            headers={"Retry-After": "1"},
        )
    if _is_postgres_deadlock(exc):
        context = _request_log_context(request, time.monotonic())
        context.update(
            {
                "status_code": 503,
                "error_class": "DatabaseDeadlockRetryable",
            },
        )
        logger.warning("api_database_deadlock_retryable", extra=context)
        _record_request_error(context)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "temporary database contention; retry the request",
                "request_id": context["request_id"],
                "error_class": "DatabaseDeadlockRetryable",
            },
            headers={"Retry-After": "1"},
        )
    context = _request_log_context(request, time.monotonic())
    context.update(
        {
            "status_code": 500,
            "error_class": type(exc).__name__,
        },
    )
    logger.exception("api_unhandled_exception", extra=context)
    _record_request_error(context)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "internal server error",
            "request_id": context["request_id"],
            "error_class": type(exc).__name__,
        },
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    context = _request_log_context(request, time.monotonic())
    errors = [
        {key: value for key, value in error.items() if key != "ctx"} for error in exc.errors()
    ]
    context.update(
        {
            "status_code": 422,
            "error_class": "RequestValidationError",
            "validation_error_count": len(errors),
        },
    )
    logger.warning("api_request_validation_failed", extra=context)
    _record_request_error(context)
    return JSONResponse(
        status_code=422,
        content={
            "detail": "request validation failed",
            "request_id": context["request_id"],
            "errors": errors,
            "error_class": "RequestValidationError",
        },
    )
