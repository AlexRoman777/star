#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
release=${1:-$(cat "$ROOT/RELEASE")}
case "$release" in
  v[0-9]*) ;;
  *) echo "release must look like v198" >&2; exit 2 ;;
esac

source_dir="$ROOT/frontend/test-results/playwright-brave"
[ -d "$source_dir" ] || {
  echo "No Playwright screenshots found to archive." >&2
  exit 0
}

run_stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$ROOT/private-benchmarks/ui-release-screenshots/$release/$run_stamp"
mkdir -p "$destination"

count=0
find "$source_dir" -type f -name '*.png' -print | while IFS= read -r source; do
  relative=${source#"$source_dir"/}
  target="$destination/$relative"
  mkdir -p "$(dirname "$target")"
  cp -p "$source" "$target"
  count=$((count + 1))
  printf '%s\n' "$count" >"$destination/.count"
done

if [ -f "$destination/.count" ]; then
  count=$(cat "$destination/.count")
  rm -f "$destination/.count"
else
  count=0
fi
printf 'Archived %s Playwright screenshots in %s\n' "$count" "$destination"
