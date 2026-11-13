#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

require_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required security tool is not installed: $1" >&2
    exit 1
  }
}

require_tool checkov
require_tool trivy

# Use only policies installed with Checkov. Never send repository metadata to
# Prisma Cloud or download external modules during this release gate.
checkov -d . \
  --framework dockerfile github_actions secrets \
  --skip-download \
  --skip-results-upload \
  --quiet \
  --compact \
  --skip-path frontend/node_modules \
  --skip-path frontend/dist \
  --skip-path Deployment/Backups \
  --skip-path Deployment/coordinator \
  --skip-path artifacts \
  --skip-path cache \
  --skip-path data \
  --skip-path models \
  --skip-path private \
  --skip-path private-benchmarks

# Reuse local databases and policies. This invocation performs no network
# update and never uploads source, media, prompts, or scan results.
trivy fs . \
  --skip-db-update \
  --skip-check-update \
  --scanners vuln,secret,misconfig \
  --severity HIGH,CRITICAL \
  --ignore-unfixed \
  --exit-code 1 \
  --skip-dirs frontend/node_modules \
  --skip-dirs frontend/dist \
  --skip-dirs Deployment/Backups \
  --skip-dirs Deployment/coordinator \
  --skip-dirs artifacts \
  --skip-dirs cache \
  --skip-dirs data \
  --skip-dirs models \
  --skip-dirs private \
  --skip-dirs private-benchmarks
