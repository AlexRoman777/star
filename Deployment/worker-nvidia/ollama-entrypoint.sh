#!/bin/sh
set -eu

models="${STAR_OLLAMA_MODELS:?Star Ollama models are required}"

OLLAMA_HOST=0.0.0.0:11434 ollama serve &
server_pid=$!

stop_server() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap stop_server INT TERM EXIT

until OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
    exit 1
  fi
  sleep 2
done

old_ifs=$IFS
# nosemgrep: ifs-tampering -- saved and restored around one split
IFS=,
for model in $models; do
  model=$(printf '%s' "$model" | tr -d ' ')
  if [ -n "$model" ]; then
    OLLAMA_HOST=127.0.0.1:11434 ollama pull "$model"
  fi
done
# nosemgrep: ifs-tampering -- restores the saved value
IFS=$old_ifs
wait "$server_pid"
