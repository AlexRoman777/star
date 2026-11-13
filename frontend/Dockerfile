# syntax=docker/dockerfile:1.19

FROM node:26.13.0-trixie-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS build
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
WORKDIR /app
RUN corepack enable \
    && corepack install --global pnpm@11.22.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=star-pnpm,target=/pnpm/store,sharing=locked \
    pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

FROM nginx:1.31.8-trixie@sha256:165ddde8751e75a1c907d24ef10d9f6deb08b57e0fe56caf67512486aff76d82
ARG STAR_RELEASE_VERSION=unversioned
ARG STAR_BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.version="$STAR_RELEASE_VERSION" \
    org.opencontainers.image.revision="$STAR_BUILD_GIT_SHA"
RUN apt-get update \
    && apt-get install --no-install-recommends --yes curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /srv \
    && chown -R 101:101 /srv
COPY --from=build --chown=101:101 /app/dist /srv
COPY --chown=101:101 frontend/nginx.conf /etc/nginx/nginx.conf
USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8080/"]
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
EXPOSE 8080
