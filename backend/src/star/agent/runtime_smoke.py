from __future__ import annotations

import io

from PIL import Image

from star.agent.inference_tasks import _highlight_worker_envelope
from star.agent.media import _build_group_contact_sheet, _prepare_model_image
from star.inference.ollama import OllamaLocalProvider
from star.inference.provider import LocalMediaInput, StructuredInferenceRequest


def _synthetic_candidate(color: tuple[int, int, int]) -> LocalMediaInput:
    source = io.BytesIO()
    Image.new("RGB", (1280, 800), color).save(source, format="WEBP", quality=80)
    return _prepare_model_image(source.getvalue(), "thumbnail", max_edge=720)


async def smoke_qwen_runtime(
    *,
    base_url: str,
    allowed_hosts: tuple[str, ...],
    model_key: str,
) -> None:
    """Exercise the released Qwen media and JSON envelope without private media.

    The gate must measure the envelope this machine is configured for, not a
    larger one: the 8 GiB NVIDIA runs Highlight comparisons at a 1344-pixel
    sheet inside 4096 tokens, and asserting an 8192-token envelope there made
    the release gate fail on a model the deployment never asks for that way.
    """

    context_tokens, sheet_edge = _highlight_worker_envelope()
    media = _build_group_contact_sheet(
        [
            _synthetic_candidate((190, 35, 35)),
            _synthetic_candidate((35, 190, 35)),
            _synthetic_candidate((35, 35, 190)),
            _synthetic_candidate((190, 190, 35)),
        ],
        sheet_edge=sheet_edge,
    )
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_index",
            "quality",
            "provisional_rating",
            "summary",
            "confidence",
        ],
        "properties": {
            "candidate_index": {"type": "integer", "minimum": 1, "maximum": 4},
            "quality": {"type": "integer", "minimum": 0, "maximum": 100},
            "provisional_rating": {"type": "integer", "minimum": 1, "maximum": 5},
            "summary": {"type": "string", "minLength": 1, "maxLength": 96},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ranking"],
        "properties": {
            "ranking": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": candidate,
            }
        },
    }
    request = StructuredInferenceRequest(
        model_key=model_key,
        task="group_compare",
        media=(media,),
        schema=schema,
        context={
            "group_type": "release_smoke",
            "candidate_count": 4,
            "media_layout": "single_labelled_contact_sheet",
            "candidate_context": [],
            "comparison_contract": {
                "purpose": "validate the released local image envelope",
                "instructions": [
                    "Return every visible numbered candidate exactly once.",
                    "Keep each summary factual and under 96 characters.",
                ],
            },
        },
        max_completion_tokens=640,
        context_tokens_override=context_tokens,
    )
    async with OllamaLocalProvider(
        allowed_models=(model_key,),
        base_url=base_url,
        allowed_hosts=allowed_hosts,
        keep_alive="30m",
        timeout_seconds=180.0,
        require_full_accelerator_residency=True,
    ) as provider:
        result = await provider.infer(request)
    ranking = result.value.get("ranking")
    if not isinstance(ranking, list) or {
        item.get("candidate_index") for item in ranking if isinstance(item, dict)
    } != {1, 2, 3, 4}:
        raise RuntimeError("Qwen runtime smoke did not return every candidate exactly once")
