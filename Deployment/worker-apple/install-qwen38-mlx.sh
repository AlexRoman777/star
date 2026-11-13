#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
APP_SUPPORT_ROOT="$HOME/Library/Application Support/Star"
LEGACY_APP_SUPPORT_ROOT="$HOME/Library/Application Support/Star"
if [[ ! -e "$APP_SUPPORT_ROOT" && -d "$LEGACY_APP_SUPPORT_ROOT" ]]; then
  mv "$LEGACY_APP_SUPPORT_ROOT" "$APP_SUPPORT_ROOT"
  echo "Migrated existing local worker state to: $APP_SUPPORT_ROOT"
fi
RUNTIME_ROOT="$APP_SUPPORT_ROOT/qwen38-mlx-runtime"
MODEL_ARCHIVE_ROOT="$APP_SUPPORT_ROOT/model-archives"
MODEL_8BIT_ROOT="$MODEL_ARCHIVE_ROOT/Qwen3.8-27B-8bit"
MODEL_4BIT_ROOT="$MODEL_ARCHIVE_ROOT/Qwen3.8-27B-4bit"
MODEL_ACTIVE_ROOT="$MODEL_ARCHIVE_ROOT/Qwen3.8-27B-active"
VENV="$RUNTIME_ROOT/.venv"
PLIST="$HOME/Library/LaunchAgents/io.star.qwen38-mlx.plist"

MODEL_ROOT="$MODEL_8BIT_ROOT"
if [[ -f "$MODEL_ROOT/config.json" \
  && -f "$MODEL_ROOT/model.safetensors.index.json" \
  && -f "$MODEL_ROOT/.star-source.json" ]]; then
  FIT_REPORT="$MODEL_ROOT/.star-fit.json"
  CURRENT_CONFIG_DIGEST=$(
    /bin/cat \
      "$MODEL_ROOT/config.json" \
      "$MODEL_ROOT/model.safetensors.index.json" \
      "$MODEL_ROOT/.star-source.json" \
      | /usr/bin/shasum -a 256 \
      | /usr/bin/awk '{print $1}'
  )
  if [[ ! -f "$FIT_REPORT" ]] \
    || [[ "$(/usr/bin/plutil -extract passed raw -o - "$FIT_REPORT" 2>/dev/null || true)" != "true" ]] \
    || [[ "$(/usr/bin/plutil -extract revision raw -o - "$FIT_REPORT" 2>/dev/null || true)" != "815b83c0df8ffd1d1b5244cf75fd6ef14fca9ef9" ]] \
    || [[ "$(/usr/bin/plutil -extract quantization_bits raw -o - "$FIT_REPORT" 2>/dev/null || true)" != "8" ]] \
    || [[ "$(/usr/bin/plutil -extract config_digest raw -o - "$FIT_REPORT" 2>/dev/null || true)" != "$CURRENT_CONFIG_DIGEST" ]]; then
    echo "Qwen3.8 27B 8-bit is present but has no passing pinned fit report; using 4-bit." >&2
    MODEL_ROOT="$MODEL_4BIT_ROOT"
  fi
else
  MODEL_ROOT="$MODEL_4BIT_ROOT"
fi
test -f "$MODEL_ROOT/config.json" || {
  echo "Missing Qwen3.8 MLX model under $MODEL_ARCHIVE_ROOT" >&2
  exit 1
}
MODEL_BITS=$(/usr/bin/plutil -extract quantization.bits raw -o - "$MODEL_ROOT/config.json")
[[ "$MODEL_BITS" == "4" || "$MODEL_BITS" == "8" ]] || {
  echo "Qwen3.8 must use a pinned 4-bit or 8-bit MLX artifact; found ${MODEL_BITS}-bit." >&2
  exit 1
}
MODEL_KIB=$(/usr/bin/du -sk "$MODEL_ROOT" | /usr/bin/awk '{print $1}')
if [[ "$MODEL_BITS" == "8" ]]; then
  (( MODEL_KIB >= 27 * 1024 * 1024 && MODEL_KIB <= 34 * 1024 * 1024 )) || {
    echo "Qwen3.8 8-bit artifact size is outside the expected 27-34 GiB envelope." >&2
    exit 1
  }
else
  (( MODEL_KIB >= 14 * 1024 * 1024 && MODEL_KIB <= 18 * 1024 * 1024 )) || {
    echo "Qwen3.8 4-bit artifact size is outside the expected 14-18 GiB envelope." >&2
    exit 1
  }
fi
PHYSICAL_MEMORY_BYTES=$(/usr/sbin/sysctl -n hw.memsize)
(( PHYSICAL_MEMORY_BYTES >= 45 * 1024 * 1024 * 1024 )) || {
  echo "Qwen3.8 27B requires a 48 GiB-class Apple Silicon host." >&2
  exit 1
}
mkdir -p "$RUNTIME_ROOT" "$HOME/Library/LaunchAgents" "$MODEL_ARCHIVE_ROOT"
ln -sfn "$MODEL_ROOT" "$MODEL_ACTIVE_ROOT"
# Record the verified archive checksum for the worker. The archive is outside the
# worker container, so the worker reports this digest with its model inventory
# and the coordinator can enable the MLX registration without a manual approval.
ACTIVE_CONFIG_DIGEST=$(
  /bin/cat \
    "$MODEL_ROOT/config.json" \
    "$MODEL_ROOT/model.safetensors.index.json" \
    $( [[ -f "$MODEL_ROOT/.star-source.json" ]] && printf '%s' "$MODEL_ROOT/.star-source.json" ) \
    | /usr/bin/shasum -a 256 \
    | /usr/bin/awk '{print $1}'
)
printf '%s\n' "$ACTIVE_CONFIG_DIGEST" > "$RUNTIME_ROOT/artifact-checksum"
APPLE_ENV="$(cd "$(dirname "$0")" && pwd)/.env"
if [[ -f "$APPLE_ENV" ]]; then
  if /usr/bin/grep -q '^STAR_MLX_ADVISER_ARTIFACT_CHECKSUM=' "$APPLE_ENV"; then
    /usr/bin/sed -i '' "s|^STAR_MLX_ADVISER_ARTIFACT_CHECKSUM=.*|STAR_MLX_ADVISER_ARTIFACT_CHECKSUM=sha256:$ACTIVE_CONFIG_DIGEST|" "$APPLE_ENV"
  else
    printf 'STAR_MLX_ADVISER_ARTIFACT_CHECKSUM=sha256:%s\n' "$ACTIVE_CONFIG_DIGEST" >> "$APPLE_ENV"
  fi
  echo "Recorded Qwen3.8 MLX artifact checksum in $APPLE_ENV"
fi
chmod 700 "$RUNTIME_ROOT"
if [[ ! -s "$RUNTIME_ROOT/token" ]]; then
  openssl rand -hex 32 > "$RUNTIME_ROOT/token"
  chmod 600 "$RUNTIME_ROOT/token"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import mlx_vlm, jinja2' >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install 'mlx-vlm>=0.6.8,<0.7' 'jinja2>=3.1.6,<4'
fi
install -m 700 "$SCRIPT_DIR/qwen38-mlx-supervisor.sh" "$RUNTIME_ROOT/supervisor.sh"
TEMP_PLIST=$(mktemp "${TMPDIR:-/tmp}/star-qwen38.XXXXXX")
trap 'rm -f "$TEMP_PLIST"' EXIT
sed -e "s|__SUPERVISOR__|$RUNTIME_ROOT/supervisor.sh|g" \
  "$SCRIPT_DIR/io.star.qwen38-mlx.plist" > "$TEMP_PLIST"
service="gui/$UID/io.star.qwen38-mlx"
if [[ -f "$PLIST" ]] && cmp -s "$TEMP_PLIST" "$PLIST" && launchctl print "$service" >/dev/null 2>&1; then
  echo "Qwen3.8 MLX adviser service is already installed and running."
else
  install -m 600 "$TEMP_PLIST" "$PLIST"
  launchctl bootout "$service" 2>/dev/null || true
  for delay in 0 1 2 3; do
    if (( delay > 0 )); then
      sleep "$delay"
    fi
    if launchctl bootstrap "gui/$UID" "$PLIST"; then
      launchctl kickstart -k "$service"
      break
    fi
    if (( delay == 3 )); then
      echo "Unable to register Qwen3.8 MLX adviser service after bounded retries." >&2
      exit 1
    fi
  done
fi
for attempt in {1..60}; do
  if curl --fail --silent --show-error --max-time 3 \
    -H "Authorization: Bearer $(<"$RUNTIME_ROOT/token")" \
    http://127.0.0.1:8772/v1/models >/dev/null; then
    echo "Qwen3.8 MLX adviser is ready."
    break
  fi
  if (( attempt == 60 )); then
    echo "Qwen3.8 MLX adviser did not become healthy within 60 seconds." >&2
    exit 1
  fi
  sleep 1
done
echo "Qwen3.8 MLX ${MODEL_BITS}-bit runtime installed; its private token remains protected."
