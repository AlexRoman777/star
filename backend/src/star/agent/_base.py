from __future__ import annotations

import hashlib
import json
import logging

from star.domain.schemas import DeepVisualObservationOutput, StarRatingV1
from star.inference.policy import compact_generation_schema
from star.inference.provider import (
    StructuredInferenceResult,
)


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _inference_accounting(
    result: StructuredInferenceResult,
    *,
    phase: str,
    input_digest: str,
    image_count: int,
    prompt_version: str,
    schema_version: str,
    include_settings: bool = False,
) -> dict[str, object]:
    """Sanitized per-invocation accounting.

    Production submissions validate against the fixed
    ``InferenceInvocationSubmission`` shape, so the provider's effective
    sampling/thinking settings are attached only where the receiving contract
    is free-form (the benchmark).
    """

    if include_settings:
        return {
            **_inference_accounting(
                result,
                phase=phase,
                input_digest=input_digest,
                image_count=image_count,
                prompt_version=prompt_version,
                schema_version=schema_version,
            ),
            "settings": dict(result.effective_settings or {}),
        }
    return {
        "phase": phase,
        "model_reported_version": result.model_version,
        "runtime": result.runtime,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "input_digest": input_digest,
        "output_digest": _digest_json(result.value),
        "image_count": image_count,
        "request_count": result.request_count,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": result.duration_ms,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "timings_ms": result.timings_ms or {},
    }


logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    pass


class TaskCancelledError(RuntimeError):
    pass


TASK_HEARTBEAT_SECONDS = 2
TASK_HEARTBEAT_FAILURE_LIMIT = 90


# The lean generation contract is the only shape a rating model is asked to
# produce; the wide retained model validates it on ingestion.
ASSET_VLM_SCHEMA = compact_generation_schema(StarRatingV1.model_json_schema())


DEEP_SIGNIFICANCE_SCHEMA = DeepVisualObservationOutput.model_json_schema()


TECHNICAL_CAPABILITIES = {"cpu-light", "image-quality", "metadata", "technical"}


MODEL_CAPABILITIES = {
    "large-vlm",
    "medium-vlm",
    "mlx",
    "multi-image-review",
    "small-vlm",
}
