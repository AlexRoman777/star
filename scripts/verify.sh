#!/bin/sh
# Star verification gate.
#
# Default: the fast gate a single-owner, self-hosted app needs before building
# images: lint, strict types, the backend suite, frontend lint, tests, and a
# production build. Runs in about half a minute.
#
#   scripts/verify.sh            fast gate (used by tools/star build/release)
#   scripts/verify.sh --full     also: the Docker PostgreSQL suite, the Brave visual
#                                suite and a compose config check (migrations, layout
#                                or compose changes only)
#   STAR_VERIFY_FULL=1     same as --full
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

FULL=${STAR_VERIFY_FULL:-0}
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --fast) FULL=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

run_pnpm() {
  if command -v pnpm >/dev/null 2>&1; then
    pnpm "$@"
  else
    npx --yes pnpm@11.22.0 "$@"
  fi
}

run_pnpm_advisory() {
  set +e
  run_pnpm "$@"
  status=$?
  set -e
  if [ "$status" -gt 1 ]; then
    return "$status"
  fi
  return 0
}

echo "verify: fast gate"
./scripts/check-star-brand.sh
uv sync --frozen --all-groups
run_pnpm --dir frontend install --frozen-lockfile

# The backend and the frontend do not share anything; run them side by side
# and the gate takes as long as the slower of the two (about 25 seconds).
backend_log=$(mktemp "${TMPDIR:-/tmp}/star-verify-backend.XXXXXX")
frontend_log=$(mktemp "${TMPDIR:-/tmp}/star-verify-frontend.XXXXXX")
trap 'rm -f "$backend_log" "$frontend_log"' EXIT HUP INT TERM
(
  uv run ruff check backend \
  && uv run mypy backend/src \
  && uv run pytest -q --ignore=backend/tests/postgres
) >"$backend_log" 2>&1 &
backend_pid=$!
(
  run_pnpm --dir frontend run lint \
  && run_pnpm --dir frontend run test \
  && run_pnpm --dir frontend run build
) >"$frontend_log" 2>&1 &
frontend_pid=$!
backend_status=0
frontend_status=0
wait "$backend_pid" || backend_status=$?
wait "$frontend_pid" || frontend_status=$?
cat "$backend_log"
cat "$frontend_log"
[ "$backend_status" -eq 0 ] || { echo "verify: backend gate failed" >&2; exit "$backend_status"; }
[ "$frontend_status" -eq 0 ] || { echo "verify: frontend gate failed" >&2; exit "$frontend_status"; }
git diff --check

if [ "$FULL" != "1" ]; then
  echo "verify: fast gate passed (run with --full for migrations, audits, visual, and scanners)"
  exit 0
fi

echo "verify: full gate"
./scripts/test-postgres.sh
run_pnpm --dir frontend run test:visual:brave
for machine in coordinator worker-nvidia worker-apple; do
  docker compose --env-file "Deployment/$machine/.env.example" \
    -f "Deployment/$machine/compose.yaml" config --quiet
done
echo "verify: full gate passed"
