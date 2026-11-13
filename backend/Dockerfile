# syntax=docker/dockerfile:1.19

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY --from=uv /uv /uvx /bin/
WORKDIR /app

# Resolve the locked third-party environment before copying source so ordinary
# application edits keep the expensive dependency layer cached.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,id=star-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project

COPY backend/src ./backend/src
RUN --mount=type=cache,id=star-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable --reinstall-package star

FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS runtime
ARG STAR_RELEASE_VERSION=unversioned
ARG STAR_BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.version="$STAR_RELEASE_VERSION" \
    org.opencontainers.image.revision="$STAR_BUILD_GIT_SHA"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STAR_BUILD_GIT_SHA=$STAR_BUILD_GIT_SHA \
     \
     \
    PATH=/app/.venv/bin:$PATH
RUN groupadd --gid 10001 star \
    && useradd --uid 10001 --gid star --no-create-home --home-dir /nonexistent star \
    && mkdir -p \
        /var/lib/star/artifacts \
        /var/lib/star/models \
        /var/lib/star-worker \
    && chown -R 10001:10001 /var/lib/star /var/lib/star-worker
WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 backend/alembic ./backend/alembic
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=5)"]
CMD ["uvicorn", "star.main:app", "--host", "0.0.0.0", "--port", "8000"]
