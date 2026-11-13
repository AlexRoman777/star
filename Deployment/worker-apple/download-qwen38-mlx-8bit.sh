#!/bin/zsh
set -euo pipefail

export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1

RUNTIME_ROOT="$HOME/Library/Application Support/Star/qwen38-mlx-runtime"
VENV="$RUNTIME_ROOT/.venv"
MODEL_ROOT="$HOME/Library/Application Support/Star/model-archives/Qwen3.8-27B-8bit"
REPOSITORY="mlx-community/Qwen3.8-27B-8bit"
REVISION="815b83c0df8ffd1d1b5244cf75fd6ef14fca9ef9"

mkdir -p "$RUNTIME_ROOT"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import huggingface_hub, mlx_vlm, jinja2' >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install \
    'huggingface-hub>=0.34,<2' \
    'mlx-vlm>=0.6.8,<0.7' \
    'jinja2>=3.1.6,<4'
fi
mkdir -p "$MODEL_ROOT"
"$VENV/bin/python" - "$REPOSITORY" "$REVISION" "$MODEL_ROOT" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

repository, revision, destination = sys.argv[1:]
root = Path(destination)
snapshot_download(
    repo_id=repository,
    revision=revision,
    local_dir=root,
)
(root / ".star-source.json").write_text(
    json.dumps(
        {"repository": repository, "revision": revision, "quantization_bits": 8},
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

BITS=$(/usr/bin/plutil -extract quantization.bits raw -o - "$MODEL_ROOT/config.json")
[[ "$BITS" == "8" ]] || { echo "Downloaded model does not declare 8-bit weights." >&2; exit 1; }
echo "Pinned Qwen3.8 27B 8-bit MLX snapshot downloaded to $MODEL_ROOT"
echo "Run ./benchmark-qwen38-mlx-fit.py with one private local test image before activation."
