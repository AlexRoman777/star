from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class LocalMediaInput:
    content: bytes
    media_type: str
    source: str
    asset_reference: str
    processing_mode: str = "source_derivative"
    orientation_policy: str = "provider_native"
    max_edge: int | None = None
    encoding_quality: int | None = None


@dataclass(frozen=True)
class StructuredInferenceRequest:
    model_key: str
    task: str
    media: tuple[LocalMediaInput, ...]
    schema: dict[str, Any]
    context: dict[str, Any]
    # ``low`` and ``medium`` are first-class bounded runtime efforts (the
    # benchmark and Qwen3.8 sessions use them). ``high`` remains the historical
    # internal thinking-enabled flag for older task contracts and is mapped by
    # providers to their configured safe effort; no provider ever asks a
    # runtime for high or xhigh reasoning.
    reasoning_effort: Literal["off", "low", "medium", "high"] = "off"
    max_completion_tokens: int = 4096
    visual_token_budget: int | None = None
    instruction_override: str | None = None
    instruction_source: str = "worker_default"
    rating_scale_override: str | None = None
    owner_taste_override: str | None = None
    system_prompt_override: str | None = None
    seed_override: int | None = None
    temperature_override: float | None = None
    top_p_override: float | None = None
    top_k_override: int | None = None
    min_p_override: float | None = None
    repeat_penalty_override: float | None = None
    presence_penalty_override: float | None = None
    context_tokens_override: int | None = None


@dataclass(frozen=True)
class InferenceSamplingProfile:
    """Versioned production sampling controls for reproducible local inference."""

    name: str
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    repeat_penalty: float
    # Zero means "not sent": production profiles never apply a presence
    # penalty; only an explicit request override (benchmark variants, owner
    # inference settings) turns it on.
    presence_penalty: float = 0.0
    profile_version: str = "inference-profile-v2"
    runtime: str = "unresolved"
    runtime_version: str | None = None
    model_artifact_checksum: str | None = None
    quantization: str | None = None
    reasoning_mode: str = "off"
    seed_supported: bool | None = None
    deterministic_seed: bool = True
    context_tokens: int = 8192
    max_output_tokens: int = 4096
    input_transform_version: str = "immich-derivative-fit-2048-jpeg-q80-v3"
    structured_output_mode: str = "authoritative-json-schema"

    def as_persisted(self, *, task: str) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "name": self.name,
            "task": task,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "model_artifact_checksum": self.model_artifact_checksum,
            "quantization": self.quantization,
            "reasoning_mode": self.reasoning_mode,
            "seed_supported": self.seed_supported,
            "identity_status": "pending_worker_inventory",
            "deterministic_seed": self.deterministic_seed,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "repeat_penalty": self.repeat_penalty,
            "presence_penalty": self.presence_penalty,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "input_transform_version": self.input_transform_version,
            "structured_output_mode": self.structured_output_mode,
        }


STRUCTURED_SAMPLING_V2 = InferenceSamplingProfile(
    name="structured-v2",
    temperature=0.2,
    # Keep production prose repeatable. The stable seed makes identical requests
    # reproducible where the runtime supports seeded sampling; the narrower
    # candidate set prevents unsupported nouns and narrative drift.
    top_p=0.75,
    top_k=10,
    min_p=0.0,
    repeat_penalty=1.05,
)
REASONING_SAMPLING_V2 = InferenceSamplingProfile(
    name="reasoning-v2",
    temperature=0.2,
    top_p=0.80,
    top_k=10,
    min_p=0.0,
    repeat_penalty=1.05,
    reasoning_mode="bounded",
)
COMPARISON_SAMPLING_V2 = InferenceSamplingProfile(
    name="comparison-v2",
    temperature=0.2,
    top_p=0.70,
    top_k=10,
    min_p=0.0,
    repeat_penalty=1.05,
)

_COMPARISON_TASKS = frozenset(
    {"group_compare", "highlight_compare", "highlight_league", "series_compare"}
)


def sampling_profile_for(
    request: StructuredInferenceRequest,
    *,
    is_thinking: bool,
) -> InferenceSamplingProfile:
    if is_thinking:
        profile = REASONING_SAMPLING_V2
    elif request.task in _COMPARISON_TASKS:
        profile = COMPARISON_SAMPLING_V2
    else:
        profile = STRUCTURED_SAMPLING_V2
    return replace(
        profile,
        temperature=(
            request.temperature_override
            if request.temperature_override is not None
            else profile.temperature
        ),
        top_p=request.top_p_override if request.top_p_override is not None else profile.top_p,
        top_k=request.top_k_override if request.top_k_override is not None else profile.top_k,
        min_p=request.min_p_override if request.min_p_override is not None else profile.min_p,
        repeat_penalty=(
            request.repeat_penalty_override
            if request.repeat_penalty_override is not None
            else profile.repeat_penalty
        ),
        presence_penalty=(
            request.presence_penalty_override
            if request.presence_penalty_override is not None
            else profile.presence_penalty
        ),
    )


def stable_inference_seed(request: StructuredInferenceRequest) -> int:
    """Derive a stable non-secret seed without retaining or logging private media."""

    if request.seed_override is not None:
        if isinstance(request.seed_override, bool) or not 0 <= request.seed_override <= 0x7FFFFFFF:
            raise ValueError("inference seed override must be an unsigned 31-bit integer")
        return request.seed_override

    media = [
        {
            "sha256": hashlib.sha256(item.content).hexdigest(),
            "media_type": item.media_type,
            "source": item.source,
            "asset_reference": item.asset_reference,
        }
        for item in request.media
    ]
    payload = {
        "model_key": request.model_key,
        "task": request.task,
        "schema": request.schema,
        "context": request.context,
        "reasoning_effort": request.reasoning_effort,
        "max_completion_tokens": request.max_completion_tokens,
        "visual_token_budget": request.visual_token_budget,
        "instruction_override": request.instruction_override,
        "instruction_source": request.instruction_source,
        "rating_scale_override": request.rating_scale_override,
        "system_prompt_override": request.system_prompt_override,
        "sampling_overrides": {
            "temperature": request.temperature_override,
            "top_p": request.top_p_override,
            "top_k": request.top_k_override,
            "min_p": request.min_p_override,
            "repeat_penalty": request.repeat_penalty_override,
            # Added only when set so every pre-existing request keeps the
            # exact seed it always had.
            **(
                {"presence_penalty": request.presence_penalty_override}
                if request.presence_penalty_override is not None
                else {}
            ),
        },
        "media": media,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:4], "big") & 0x7FFFFFFF


@dataclass(frozen=True)
class StructuredInferenceResult:
    value: dict[str, Any]
    model_version: str
    runtime: str
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_count: int = 1
    started_at: str | None = None
    completed_at: str | None = None
    timings_ms: dict[str, int] | None = None
    # The sampling and thinking controls the provider actually sent, so a
    # benchmark result can record what produced it. Production submissions
    # keep their fixed accounting shape and ignore this field.
    effective_settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class LocalModelInventoryItem:
    model_key: str
    version: str
    runtime: str
    quantization: str
    runtime_format: str
    architecture: str | None
    size_bytes: int | None
    max_context_length: int | None
    vision: bool
    reasoning: bool
    loaded: bool
    runtime_version: str | None = None
    artifact_checksum: str | None = None
    seed_supported: bool | None = None


class LocalInferenceProvider(Protocol):
    """Runtime-neutral contract; implementations must not accept arbitrary tools or URLs."""

    async def health(self) -> bool: ...

    async def inventory(self) -> list[LocalModelInventoryItem]: ...

    async def warmup(self, model_key: str, *, system_prompt: str | None = None) -> None: ...

    async def unload(self, model_key: str) -> None: ...

    async def infer(self, request: StructuredInferenceRequest) -> StructuredInferenceResult: ...

    async def close(self) -> None: ...
