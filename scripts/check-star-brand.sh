#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
retired=photo"pilot"

paths=$(git ls-files | grep -i -- "$retired" || true)
content=$(git grep -Iin -- "$retired" -- . || true)
if [ -n "$paths" ] || [ -n "$content" ]; then
  [ -z "$paths" ] || printf '%s\n' "$paths" >&2
  [ -z "$content" ] || printf '%s\n' "$content" >&2
  echo "Retired brand remains in tracked paths or content." >&2
  exit 1
fi

echo "brand: tracked paths and content are Star-only"
