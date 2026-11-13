#!/bin/zsh
set -euo pipefail

STATE_ROOT="${HOME}/Library/Application Support/Star"
RUNTIME_ROOT="${STATE_ROOT}/qwen38-mlx-runtime"
MODEL_ROOT="${STATE_ROOT}/model-archives/Qwen3.8-27B-active"
SERVER="${RUNTIME_ROOT}/.venv/bin/mlx_vlm.server"
TOKEN_FILE="${RUNTIME_ROOT}/token"

if [[ ! -x "${SERVER}" || ! -f "${MODEL_ROOT}/config.json" || ! -s "${TOKEN_FILE}" ]]; then
  /usr/bin/logger -t star-qwen38-mlx -- "Qwen3.8 MLX runtime is incomplete"
  exit 78
fi

exec "${SERVER}" \
  --host 127.0.0.1 \
  --port 8772 \
  --vision-cache-size 0 \
  --prefill-step-size 512 \
  --kv-bits 8 \
  --kv-group-size 64 \
  --quantized-kv-start 2048 \
  --max-kv-size 32768 \
  --max-tokens 8192 \
  --max-num-seqs 1 \
  --log-progress-interval 0 \
  --api-key "$(<"${TOKEN_FILE}")" \
  --log-level WARNING
