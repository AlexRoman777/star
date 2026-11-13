# syntax=docker/dockerfile:1.19

FROM node:24.18.0-trixie-slim@sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573 AS build
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

FROM nginx:1.30.4-trixie@sha256:5cf90903deda2c5981b8ad05e7617ac010e389f0dde0ac83487c02c509281de6
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
