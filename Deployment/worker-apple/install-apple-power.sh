#!/bin/sh
# Installs the native Apple power/host-load status service (launchd user agent)
# that the accelerator worker reads through --power-status-url, and retires the
# former Apple Vision agent, service, binary, and token name. Idempotent.
set -eu

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_SUPPORT="$HOME/Library/Application Support/Star"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LABEL="io.star.apple-power"
LEGACY_LABEL="io.star.apple-vision"

mkdir -p "$APP_SUPPORT" "$LAUNCH_AGENTS"

# Retire the former Apple Vision agent (best effort; nothing fails if it is absent).
launchctl bootout "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
rm -f "$LAUNCH_AGENTS/$LEGACY_LABEL.plist" \
  "$APP_SUPPORT/apple-vision-service.py" \
  "$APP_SUPPORT/star-vision"

cp "$SOURCE/apple-power-service.py" "$APP_SUPPORT/apple-power-service.py"
chmod 700 "$APP_SUPPORT/apple-power-service.py"

# Keep the existing shared secret so the Apple worker .env keeps working after the rename.
if [ ! -f "$APP_SUPPORT/apple-power-token" ] && [ -f "$APP_SUPPORT/apple-vision-token" ]; then
  mv "$APP_SUPPORT/apple-vision-token" "$APP_SUPPORT/apple-power-token"
  echo "Moved the former Apple Vision token to: $APP_SUPPORT/apple-power-token"
fi
if [ ! -f "$APP_SUPPORT/apple-power-token" ]; then
  umask 077
  openssl rand -hex 32 >"$APP_SUPPORT/apple-power-token"
fi
chmod 600 "$APP_SUPPORT/apple-power-token"

sed "s#__APP_SUPPORT__#$APP_SUPPORT#g" \
  "$SOURCE/io.star.apple-power.plist" \
  >"$LAUNCH_AGENTS/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for delay in 0 1 2 3; do
  if [ "$delay" -gt 0 ]; then
    sleep "$delay"
  fi
  if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS/$LABEL.plist"; then
    launchctl enable "gui/$(id -u)/$LABEL"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    break
  fi
  if [ "$delay" -eq 3 ]; then
    echo "Unable to register the Apple worker power service after bounded retries." >&2
    exit 1
  fi
done

for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl --fail --silent --show-error --max-time 2 \
    -H "Authorization: Bearer $(cat "$APP_SUPPORT/apple-power-token")" \
    http://127.0.0.1:8769/health >/dev/null; then
    echo "Apple power service is ready."
    break
  fi
  if [ "$attempt" -eq 20 ]; then
    echo "Apple power service did not become healthy within 20 seconds." >&2
    exit 1
  fi
  sleep 1
done

echo "Apple power service installed."
echo "Service token stored privately at: $APP_SUPPORT/apple-power-token"
echo "tools/star deploy Apple synchronizes it to the Apple worker .env automatically."
