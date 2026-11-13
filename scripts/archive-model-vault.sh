#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: $0 DESTINATION [--include-images] [--lmstudio-dir PATH]" >&2
  echo "DESTINATION should be a mounted NAS directory, for example /Volumes/Star/model-vault/2026-07-20." >&2
}

[[ $# -ge 1 ]] || { usage; exit 2; }
destination=$1
shift
include_images=0
lmstudio_dir="${HOME}/.lmstudio/models"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-images) include_images=1; shift ;;
    --lmstudio-dir) lmstudio_dir=$2; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$destination/volumes" "$destination/images" "$destination/lmstudio"
cp "$repo_root/models/model-lock.json" "$destination/model-lock.json"

archive_image="python:3.14.6-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6"
model_volumes=(
  star-nvidia-signal-models
  star-nvidia-ollama-models
)

for volume in "${model_volumes[@]}"; do
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Skipping absent volume: $volume"
    continue
  fi
  echo "Archiving model volume: $volume"
  docker run --rm --network none \
    --volume "$volume:/source:ro" \
    --volume "$destination/volumes:/backup" \
    "$archive_image" \
    tar --numeric-owner --xattrs --acls -czf "/backup/${volume}.tar.gz" -C /source .
done

if [[ -d "$lmstudio_dir" ]]; then
  echo "Archiving LM Studio artifacts from $lmstudio_dir"
  tar -czf "$destination/lmstudio/lmstudio-models.tar.gz" -C "$lmstudio_dir" .
else
  echo "LM Studio directory not found; skipped: $lmstudio_dir"
fi

if [[ $include_images -eq 1 ]]; then
  mapfile -t image_ids < <(
    docker compose --project-directory "$repo_root/Deployment/worker-nvidia" \
      --env-file "$repo_root/Deployment/worker-nvidia/.env" \
      -f "$repo_root/Deployment/worker-nvidia/compose.yaml" images --quiet | sort -u
  )
  if [[ ${#image_ids[@]} -gt 0 ]]; then
    echo "Archiving exact NVIDIA container images"
    docker image save --output "$destination/images/star-nvidia-images.tar" "${image_ids[@]}"
  fi
fi

(
  cd "$destination"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS
)
echo "Model vault complete: $destination"
echo "Verify later with: cd '$destination' && shasum -a 256 -c SHA256SUMS"
