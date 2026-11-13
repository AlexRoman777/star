#!/usr/bin/env python3
"""Validate the exact Qwen3.8 MLX profile without retaining family content."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlx.core as mx
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template

MAX_PEAK_MEMORY_GB = 42.0
MODEL_REVISION = "815b83c0df8ffd1d1b5244cf75fd6ef14fca9ef9"
COMMON_GENERATION = {
    "max_tokens": 64,
    "temperature": 0.0,
    "max_kv_size": 32_768,
    "kv_bits": 8,
    "kv_group_size": 64,
    "quantized_kv_start": 2_048,
    "prefill_step_size": 512,
    "enable_thinking": True,
    "thinking_budget": 256,
    "verbose": False,
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path.home()
        / "Library/Application Support/Star/model-archives/Qwen3.8-27B-8bit",
    )
    parser.add_argument("--image", type=Path, required=True)
    return parser.parse_args()


def _swap_used_mb() -> float:
    output = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    match = re.search(r"used\s*=\s*([0-9.]+)([MG])", output)
    if match is None:
        raise RuntimeError("unable to read macOS swap usage")
    value = float(match.group(1))
    return value * 1024 if match.group(2) == "G" else value


def _config_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("config.json", "model.safetensors.index.json", ".star-source.json"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _tokenizer(processor: Any) -> Any:
    value = getattr(processor, "tokenizer", processor)
    if not hasattr(value, "encode") or not hasattr(value, "decode"):
        raise RuntimeError("MLX processor does not expose a tokenizer")
    return value


def _near_window_prompt(tokenizer: Any) -> str:
    record = (
        '{"review":"ordinary family archive evidence","rating":3,'
        '"note":"bounded local calibration record"}\n'
    )
    token_ids = tokenizer.encode(record * 12_000, add_special_tokens=False)
    return tokenizer.decode(token_ids[:31_000], skip_special_tokens=True)


def _run_case(
    model: Any,
    processor: Any,
    *,
    name: str,
    prompt: str,
    images: list[str],
) -> dict[str, Any]:
    formatted = apply_chat_template(
        processor,
        model.config,
        prompt,
        num_images=len(images),
        reasoning_effort="medium",
    )
    mx.reset_peak_memory()
    started = time.perf_counter()
    result = generate(
        model,
        processor,
        formatted,
        image=images or None,
        **COMMON_GENERATION,
    )
    return {
        "name": name,
        "prompt_tokens": int(result.prompt_tokens),
        "generation_tokens": int(result.generation_tokens),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "peak_memory_gb": round(float(result.peak_memory), 3),
    }


def main() -> int:
    args = _arguments()
    if not args.image.is_file():
        raise SystemExit("--image must name one local private test image")
    config = json.loads((args.model_dir / "config.json").read_text(encoding="utf-8"))
    source = json.loads(
        (args.model_dir / ".star-source.json").read_text(encoding="utf-8")
    )
    if config.get("quantization", {}).get("bits") != 8:
        raise SystemExit("fit benchmark requires the pinned 8-bit model")
    if source.get("revision") != MODEL_REVISION:
        raise SystemExit("fit benchmark requires the pinned model revision")

    swap_before = _swap_used_mb()
    load_started = time.perf_counter()
    model, processor = load(str(args.model_dir), lazy=False, strict=True)
    load_seconds = time.perf_counter() - load_started
    tokenizer = _tokenizer(processor)
    image = str(args.image)
    cases = [
        _run_case(
            model,
            processor,
            name="text_near_32k",
            prompt=_near_window_prompt(tokenizer),
            images=[],
        ),
        _run_case(
            model,
            processor,
            name="single_image_16k_profile",
            prompt="Inspect only visible evidence and return one concise literal observation.",
            images=[image],
        ),
        _run_case(
            model,
            processor,
            name="four_image_16k_profile",
            prompt=(
                "Compare four labelled candidates and return only their "
                "strongest-to-weakest order."
            ),
            images=[image, image, image, image],
        ),
    ]
    del model
    del processor
    gc.collect()
    mx.clear_cache()
    swap_after = _swap_used_mb()
    peak = max(float(case["peak_memory_gb"]) for case in cases)
    # macOS may report a few megabytes of unrelated swap churn while the long
    # benchmark runs.  Treat any material growth as a failed fit, while keeping
    # the threshold far below one model tensor or KV-cache allocation.
    swap_growth_mb = max(0.0, swap_after - swap_before)
    passed = peak <= MAX_PEAK_MEMORY_GB and swap_growth_mb <= 64.0
    report = {
        "schema_version": 1,
        "passed": passed,
        "repository": "mlx-community/Qwen3.8-27B-8bit",
        "revision": MODEL_REVISION,
        "quantization_bits": 8,
        "config_digest": _config_digest(args.model_dir),
        "load_seconds": round(load_seconds, 3),
        "maximum_peak_memory_gb": round(peak, 3),
        "maximum_allowed_peak_memory_gb": MAX_PEAK_MEMORY_GB,
        "swap_before_mb": swap_before,
        "swap_after_mb": swap_after,
        "swap_growth_mb": swap_growth_mb,
        "cases": cases,
        "completed_at": datetime.now(UTC).isoformat(),
        "private_media_retained": False,
    }
    destination = args.model_dir / ".star-fit.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
