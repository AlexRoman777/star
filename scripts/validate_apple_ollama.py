#!/usr/bin/env python3
"""Run media-free Star vision acceptance against native Apple Ollama."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from star.agent._base import ASSET_VLM_SCHEMA
from star.agent.media import _build_group_contact_sheet, _prepare_model_image
from star.domain.schemas import StarRatingV1
from star.inference.ollama import OllamaLocalProvider
from star.inference.policy import NVIDIA_SYSTEM_PROMPT_KEY, MODEL_SYSTEM_PROMPTS
from star.inference.provider import (
    LocalMediaInput,
    StructuredInferenceRequest,
    StructuredInferenceResult,
)

MINISTRAL = "ministral-3:8b-instruct-2512-q4_K_M"
QWEN = "qwen3-vl:8b-instruct-q4_K_M"
ADVISER = "qwen3.8:27b"
LEGACY_ADVISER = "qwen3.5:27b"
ADVISER_CONTEXT_TOKENS = 196_608


def _candidate(index: int) -> LocalMediaInput:
    image = Image.new("RGB", (1280, 800), (30 + index * 19, 50 + index * 13, 80 + index * 11))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1200, 720), outline=(245, 245, 245), width=16)
    draw.ellipse((220 + index * 11, 170, 620 + index * 11, 570), fill=(220, 150, 60))
    draw.text((690, 330), f"TEST {index}", fill=(255, 255, 255), stroke_width=2)
    source = io.BytesIO()
    image.save(source, format="JPEG", quality=80)
    return _prepare_model_image(source.getvalue(), "preview", max_edge=720)


def _primary_candidate(index: int, image_paths: list[Path]) -> LocalMediaInput:
    if not image_paths:
        return _candidate(index)
    path = image_paths[(index - 1) % len(image_paths)]
    return _prepare_model_image(path.read_bytes(), "preview", max_edge=1024)


def _comparison_schema(count: int) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ranking"],
        "properties": {
            "ranking": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_index", "quality", "summary", "confidence"],
                    "properties": {
                        "candidate_index": {"type": "integer", "minimum": 1, "maximum": count},
                        "quality": {"type": "integer", "minimum": 0, "maximum": 100},
                        "summary": {"type": "string", "minLength": 1, "maxLength": 96},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                },
            }
        },
    }


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--candidates", type=int, choices=range(2, 9), default=4)
    parser.add_argument("--skip-primary", action="store_true")
    parser.add_argument("--skip-group", action="store_true")
    parser.add_argument(
        "--primary-parallel",
        type=int,
        choices=(1, 2, 4, 6, 8, 10, 12),
        default=1,
    )
    parser.add_argument("--primary-image-dir", type=Path)
    parser.add_argument("--skip-adviser", action="store_true")
    parser.add_argument("--adviser-only", action="store_true")
    parser.add_argument(
        "--adviser-model",
        choices=(ADVISER, LEGACY_ADVISER),
        default=ADVISER,
    )
    args = parser.parse_args()
    provider = OllamaLocalProvider(
        allowed_models={MINISTRAL, QWEN, args.adviser_model},
        base_url=args.base_url,
        allowed_hosts={"127.0.0.1", "localhost"},
        keep_alive="30s",
        timeout_seconds=240,
    )
    try:
        inventory = await provider.inventory()
        found = {item.model_key for item in inventory}
        required = (
            {args.adviser_model} if args.adviser_only else {MINISTRAL, QWEN, args.adviser_model}
        )
        missing = required - found
        if missing:
            raise RuntimeError(f"missing required Ollama models: {sorted(missing)}")
        vision = {item.model_key for item in inventory if item.vision}
        missing_vision = ({MINISTRAL, QWEN} if not args.adviser_only else set()) - vision
        if missing_vision:
            raise RuntimeError(f"models are not vision-capable: {sorted(missing_vision)}")

        results: list[dict[str, object]] = []
        if not args.skip_primary and not args.adviser_only:
            image_paths = (
                sorted(
                    path
                    for path in args.primary_image_dir.iterdir()
                    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                )
                if args.primary_image_dir
                else []
            )
            if args.primary_image_dir and not image_paths:
                raise RuntimeError("primary image directory contains no supported images")

            async def primary_request(index: int) -> StructuredInferenceResult:
                return await provider.infer(
                    StructuredInferenceRequest(
                        model_key=MINISTRAL,
                        task="primary_review",
                        media=(_primary_candidate(index, image_paths),),
                        schema=ASSET_VLM_SCHEMA,
                        context={
                            "review_profile": "release_acceptance",
                            "review_instruction": "Inspect the image before rating it.",
                            "technical_evidence": {},
                            "capture_facts": {},
                            "model_evidence": {},
                            "prompt_version": "apple-ollama-acceptance-v2",
                            "limitations": ["local_release_benchmark"],
                        },
                        system_prompt_override=MODEL_SYSTEM_PROMPTS[NVIDIA_SYSTEM_PROMPT_KEY],
                        reasoning_effort="off",
                        max_completion_tokens=512,
                        context_tokens_override=8192,
                    )
                )

            started = time.monotonic()
            primary_results = await asyncio.gather(
                *(primary_request(index) for index in range(1, args.primary_parallel + 1))
            )
            elapsed = time.monotonic() - started
            for primary in primary_results:
                StarRatingV1.model_validate(primary.value)
            results.append(
                {
                    "model": MINISTRAL,
                    "parallel_requests": args.primary_parallel,
                    "seconds": round(elapsed, 2),
                    "photos_per_hour": round(args.primary_parallel * 3600 / elapsed, 1),
                    "input_tokens": sum((item.input_tokens or 0) for item in primary_results),
                    "output_tokens": sum((item.output_tokens or 0) for item in primary_results),
                    "fixture_source": "local" if image_paths else "synthetic",
                }
            )
            await provider.unload(MINISTRAL)

        if not args.adviser_only and not args.skip_group:
            count = args.candidates
            media = [_candidate(index) for index in range(1, count + 1)]
            sheet = _build_group_contact_sheet(media, sheet_edge=1440 if count <= 4 else 2048)
            request_started = time.monotonic()
            result = await provider.infer(
                StructuredInferenceRequest(
                    model_key=QWEN,
                    task="group_compare",
                    media=(sheet,),
                    schema=_comparison_schema(count),
                    context={
                        "group_type": "release_acceptance",
                        "candidate_count": count,
                        "media_layout": (
                            "single_image" if count == 1 else "single_labelled_contact_sheet"
                        ),
                        "candidate_context": [],
                        "comparison_contract": {
                            "purpose": "verify every numbered synthetic candidate",
                            "instructions": ["Return every candidate exactly once."],
                        },
                    },
                    reasoning_effort="off",
                    max_completion_tokens=max(256, count * 96),
                    context_tokens_override=16384,
                )
            )
            ranking = result.value.get("ranking")
            returned = (
                {item.get("candidate_index") for item in ranking if isinstance(item, dict)}
                if isinstance(ranking, list)
                else set()
            )
            if returned != set(range(1, count + 1)):
                raise RuntimeError(f"Qwen omitted candidates at count {count}")
            results.append(
                {
                    "model": QWEN,
                    "candidates": count,
                    "seconds": round(time.monotonic() - request_started, 2),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
            )
            await provider.unload(QWEN)

        if not args.skip_adviser:
            adviser_started = time.monotonic()
            adviser = await provider.infer(
                StructuredInferenceRequest(
                    model_key=args.adviser_model,
                    task="preference_prompt_synthesis",
                    media=(),
                    schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary", "safe_to_propose"],
                        "properties": {
                            "summary": {"type": "string", "minLength": 1, "maxLength": 160},
                            "safe_to_propose": {"type": "boolean"},
                        },
                    },
                    context={
                        "instruction": (
                            "Confirm that the synthetic evidence is insufficient for a real "
                            "prompt change. Return a concise summary and safe_to_propose=false."
                        ),
                        "synthetic_evidence": {
                            "accepted_reviews": 3,
                            "owner_ratings": 2,
                            "exact_agreements": 2,
                        },
                    },
                    reasoning_effort="high",
                    max_completion_tokens=4096,
                    context_tokens_override=ADVISER_CONTEXT_TOKENS,
                )
            )
            if adviser.value.get("safe_to_propose") is not False:
                raise RuntimeError("Qwen adviser ignored the synthetic safety contract")
            results.append(
                {
                    "model": args.adviser_model,
                    "candidates": 0,
                    "seconds": round(time.monotonic() - adviser_started, 2),
                    "input_tokens": adviser.input_tokens,
                    "output_tokens": adviser.output_tokens,
                    "context_tokens": ADVISER_CONTEXT_TOKENS,
                }
            )
            await provider.unload(args.adviser_model)
        print(json.dumps({"status": "passed", "results": results}, separators=(",", ":")))
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(_run())
