# syntax=docker/dockerfile:1.19

FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv

FROM python:3.14.6-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY --from=uv /uv /bin/uv
WORKDIR /build
COPY requirements.lock ./requirements.lock
COPY Package/star-*.whl /build/
RUN --mount=type=cache,id=star-standalone-worker-uv,target=/root/.cache/uv,sharing=locked \
    uv venv /app/.venv \
    && uv pip sync --python /app/.venv/bin/python requirements.lock \
    && uv pip install --python /app/.venv/bin/python --no-deps ./star-*.whl \
    && /app/.venv/bin/star-agent --help >/dev/null \
    && /app/.venv/bin/star-agent validate-contracts

FROM python:3.14.6-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
# checkov:skip=CKV_DOCKER_2:This outbound worker has no HTTP health endpoint; authenticated coordinator heartbeats are the health contract and PID 1 exit is handled by Docker restart policy.
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
    && mkdir -p /var/lib/star-worker \
    && chown 10001:10001 /var/lib/star-worker
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/star-agent"]
CMD ["--help"]
