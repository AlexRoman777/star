from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import time
from typing import Any

import httpx
from PIL import Image
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from star.agent._base import (
    ASSET_VLM_SCHEMA,
    LeaseLostError,
    TaskCancelledError,
    _digest_json,
    _inference_accounting,
    logger,
)
from star.agent.identity import _maintain_lease, _renew_lease, _submit_failure
from star.agent.media import (
    MediaDecodeError,
    _build_group_contact_sheet,
    _media_provenance,
    _prepare_model_image,
    _validated_group_asset_ids,
)
from star.analysis.fingerprints import UnsafeImageError, compute_visual_fingerprints
from star.domain.schemas import (
    StarRatingV1,
)
from star.domain.schemas.library import InferenceStage, InferenceStageSettings
from star.domain.schemas.library_review_schemas import LIBRARY_REVIEW_SCHEMA
from star.inference.ollama import LocalModelOutOfMemoryError
from star.inference.policy import (
    ALLOWED_TASKS,
    APPLE_SYSTEM_PROMPT_KEY,
    IMAGE_FREE_TASKS,
    MODEL_SYSTEM_PROMPTS,
    NVIDIA_SYSTEM_PROMPT_KEY,
    OWNER_TASTE_PROMPT_KEY,
    RATING_SCALE_PROMPT_KEY,
    LocalInferenceContextWindowError,
    LocalInferenceEnvelopeError,
    LocalInferenceError,
    LocalInferenceRequestError,
    LocalInferenceSchemaError,
    LocalInferenceTransportError,
    PhotoComputeBudgetExceeded,
)
from star.inference.provider import (
    LocalInferenceProvider,
    LocalMediaInput,
    StructuredInferenceRequest,
    StructuredInferenceResult,
    sampling_profile_for,
    stable_inference_seed,
)
from star.services.pipeline_contract import (
    HIGHLIGHT_PROMPT_VERSION,
    TASK_RUNTIME_CONTRACTS,
)
from star.services.task_deadlines import task_wall_seconds

PRIMARY_VISUAL_MAX_COMPLETION_TOKENS = 512
PRIMARY_REVIEW_CONTEXT_TOKENS = 6144
# The senior model answers directly by default (owner decision, 6 Sep 2026); the
# guard covers the compact answer plus repair headroom. Thinking is opt-in via
# the manifest inference settings.
SENIOR_REVIEW_MAX_COMPLETION_TOKENS = 1024
SENIOR_REVIEW_CONTEXT_TOKENS = 16_384
STAR_RATING_SCHEMA_VERSION = "star-rating-v1"
GROUP_COMPARISON_MAX_COMPLETION_TOKENS = 256
HIGHLIGHT_COMPARISON_RUNTIME = TASK_RUNTIME_CONTRACTS["group.highlight_compare"]
if HIGHLIGHT_COMPARISON_RUNTIME.max_media_edge is None:
    raise RuntimeError("highlight comparison runtime contract is incomplete")
if HIGHLIGHT_COMPARISON_RUNTIME.context_tokens is None:
    raise RuntimeError("highlight comparison runtime contract is incomplete")
if HIGHLIGHT_COMPARISON_RUNTIME.contact_sheet_edge is None:
    raise RuntimeError("highlight comparison runtime contract is incomplete")
HIGHLIGHT_COMPARISON_MAX_MEDIA_EDGE = HIGHLIGHT_COMPARISON_RUNTIME.max_media_edge
HIGHLIGHT_COMPARISON_CONTEXT_TOKENS = HIGHLIGHT_COMPARISON_RUNTIME.context_tokens
HIGHLIGHT_COMPARISON_CONTACT_SHEET_EDGE = HIGHLIGHT_COMPARISON_RUNTIME.contact_sheet_edge
GROUP_VLM_TASK_TYPES = frozenset({"group.highlight_compare", "group.series_select"})
ASSET_VLM_TASK_TYPES = frozenset({"asset.primary_review", "asset.deep_review"})
# The nightly reading of the library: image free, administrative, and never part
# of any photograph's completion.
LIBRARY_REVIEW_TASK_TYPE = "library.review"
LIBRARY_REVIEW_MODEL_KEY = "qwen3.8:27b"
LIBRARY_REVIEW_MAX_COMPLETION_TOKENS = 1536
LIBRARY_REVIEW_CONTEXT_TOKENS = 16_384
LIBRARY_REVIEW_COMPUTE_BUDGET_MS = 600_000
WORKER_EXECUTABLE_TASK_TYPES = frozenset(
    {
        "asset.media_stage",
        LIBRARY_REVIEW_TASK_TYPE,
        "asset.semantic_embedding",
        "worker.model_warmup",
        *GROUP_VLM_TASK_TYPES,
        *ASSET_VLM_TASK_TYPES,
    }
)
WORKER_INFERENCE_TASKS = frozenset(
    {
        "group_compare",
        "primary_review",
        "photo_deep_review",
    }
)

RESULT_SUBMISSION_ATTEMPTS = 8
# Hang guard for inference calls that carry no explicit compute budget; long
# preference synthesis passes its own deadline and is unaffected.
DEFAULT_INFERENCE_GUARD_SECONDS = 900
RESULT_SUBMISSION_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
RESULT_SUBMISSION_DEADLINE_SECONDS = 180.0
RESULT_SUBMISSION_REQUEST_TIMEOUT_SECONDS = 30.0


def _leased_context_tokens(target: dict[str, Any], *, default: int) -> int:
    """Return the context window to request, never larger than this release allows.

    A task keeps the envelope it was queued with, which is what lets a fallback
    retry ask for less. It must not ask for more: a queue built before an
    envelope was reduced would otherwise keep demanding the old window until it
    drained. On the 8 GiB NVIDIA that means Ollama offloads part of the model
    to the CPU on every one of those photos and the review fails. Leaving the
    value unset is not an option either, because the model package default can
    allocate a much larger cache per parallel runner and ruin six-lane
    throughput.
    """

    requested = target.get("context_tokens")
    if isinstance(requested, int) and not isinstance(requested, bool):
        return min(requested, default)
    return default


async def _cache_normalized_media(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    *,
    asset_id: str,
    media: LocalMediaInput,
) -> None:
    """Persist a clean JPEG opportunistically without making cache health a completion voter."""

    try:
        response = await client.post(
            f"/api/v1/workers/tasks/{task['task_id']}/normalized-media/{asset_id}",
            headers={
                "X-Star-Lease": str(task["lease_id"]),
                "Content-Type": "image/jpeg",
                "X-Star-Encoding-Quality": "80",
            },
            content=media.content,
            timeout=30.0,
        )
        if response.status_code not in {200, 201, 204}:
            logger.warning(
                "normalized media cache write skipped",
                extra={
                    "task_type": str(task.get("task_type")),
                    "status_code": response.status_code,
                    "error_class": "NormalizedMediaCacheRejected",
                },
            )
    except httpx.RequestError as error:
        logger.warning(
            "normalized media cache write unavailable",
            extra={
                "task_type": str(task.get("task_type")),
                "error_class": "NormalizedMediaCacheUnavailable",
                # The transport failure is the only clue to whether the cache is
                # simply slow under six lanes or actually unreachable.
                "transport_error": type(error).__name__,
            },
        )


async def _drain_cache_tasks(tasks: list[asyncio.Task[None]], *, cancel: bool = False) -> None:
    """Settle background cache uploads without letting cache health vote on completion."""

    if cancel:
        for pending in tasks:
            pending.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class SourceMediaUnavailableError(RuntimeError):
    """The coordinator could not provide one task-owned Immich derivative."""

    def __init__(self, error_code: str, asset_id: str | None) -> None:
        super().__init__("source media is unavailable")
        self.error_code = error_code
        self.asset_id = asset_id


def _leaf_task_failure(group_failure: BaseExceptionGroup[BaseException]) -> BaseException:
    """Unwrap one concrete failure so type-based retryability classification survives."""

    failure: BaseException = group_failure
    while isinstance(failure, BaseExceptionGroup):
        failure = failure.exceptions[0]
    return failure


def _target_media_edge(target: dict[str, Any], default: int = 2048) -> int:
    """Honor a coordinator-applied fallback media edge within worker bounds."""

    value = target.get("max_media_edge")
    if isinstance(value, int) and not isinstance(value, bool) and 128 <= value <= 2048:
        return value
    return default


def _apply_retry_sampling_seed(
    request: StructuredInferenceRequest,
    target: dict[str, Any],
) -> StructuredInferenceRequest:
    """Give a structured-output retry a varied, bounded, reproducible seed.

    The coordinator advances ``retry_sampling_seed_index`` when it schedules a
    JSON/schema-failure replay; without it, temperature-0.2 seeded sampling
    reproduces the same invalid output byte for byte.
    """

    raw_index = target.get("retry_sampling_seed_index")
    if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index <= 0:
        return request
    base_seed = stable_inference_seed(request)
    return dataclasses.replace(request, seed_override=(base_seed + min(raw_index, 8)) & 0x7FFFFFFF)


def _task_asset_id(task: dict[str, Any]) -> str | None:
    """Resolve the single asset a task owns so source failures stay attributable."""

    for candidate in (task.get("asset_id"), (task.get("target_ref") or {}).get("asset_id")):
        if candidate is not None:
            return str(candidate)
    return None


async def _fetch_media_response(
    client: httpx.AsyncClient,
    *,
    path: str,
    lease_id: str,
    asset_id: str | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Fetch one coordinator media payload, honoring the source-failure header.

    Every task-owned media download must pass through here (or a wrapper) so a
    coordinator-detected unusable derivative raises ``SourceMediaUnavailableError``
    and feeds the source-failure quarantine counter instead of surfacing as a
    generic retryable transport failure.
    """

    response = await client.get(path, params=params, headers={"X-Star-Lease": lease_id})
    source_error = response.headers.get("x-star-media-error")
    from star.services.source_media import PERMANENT_SOURCE_ERROR_CODES

    if asset_id is None and "/media/" in path:
        asset_id = path.rsplit("/", 1)[-1]
    if source_error in PERMANENT_SOURCE_ERROR_CODES:
        raise SourceMediaUnavailableError(source_error, asset_id)
    if response.status_code == 404:
        # Immich (or Star's own record) has no derivative for this photo. Every
        # download through here is task-owned media, so a 404 is a source
        # condition, reported as one: classified and counted under Immich, not
        # as a rejected worker payload.
        raise SourceMediaUnavailableError(source_error or "asset_unavailable", asset_id)
    response.raise_for_status()
    return response


async def _prepared_media_input(
    content: bytes,
    input_variant: str,
    max_edge: int,
    *,
    asset_id: str | None,
) -> LocalMediaInput:
    """Normalize media off the event loop, mapping permanent decode failures."""

    try:
        return await asyncio.to_thread(_prepare_model_image, content, input_variant, max_edge)
    except MediaDecodeError as exc:
        raise SourceMediaUnavailableError(exc.source_error_code, asset_id) from exc


async def _fetch_prepared_media(
    client: httpx.AsyncClient,
    *,
    path: str,
    lease_id: str,
    max_edge: int = 2048,
    asset_id: str | None = None,
) -> tuple[httpx.Response, LocalMediaInput]:
    """Fetch one coordinator media payload and normalize it off the event loop."""

    if asset_id is None and "/media/" in path:
        asset_id = path.rsplit("/", 1)[-1]
    response = await _fetch_media_response(client, path=path, lease_id=lease_id, asset_id=asset_id)
    input_variant = response.headers.get("x-star-input-variant", "preview")
    prepared = await _prepared_media_input(
        response.content, input_variant, max_edge, asset_id=asset_id
    )
    return response, prepared


async def _submit_completed_result(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    path: str,
    payload: dict[str, Any],
) -> None:
    """Submit an already-computed result with bounded, idempotent retries."""

    digest = _digest_json(payload)
    headers = {"X-Star-Result-Digest": digest}
    execution_digest = task.get("target_ref", {}).get("execution_manifest_digest")
    if isinstance(execution_digest, str):
        headers["X-Star-Execution-Manifest"] = execution_digest
    last_error: Exception | None = None
    deadline = time.monotonic() + RESULT_SUBMISSION_DEADLINE_SECONDS
    for attempt in range(RESULT_SUBMISSION_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error is not None:
                raise last_error
            raise TimeoutError("result submission deadline exhausted")
        try:
            response = await client.post(
                path,
                params={"lease_id": str(task["lease_id"])},
                json=payload,
                headers=headers,
                timeout=min(RESULT_SUBMISSION_REQUEST_TIMEOUT_SECONDS, remaining),
            )
            if response.status_code < 500 and response.status_code not in {408, 429}:
                response.raise_for_status()
                return
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_error = exc
            retryable_status = (
                isinstance(exc, httpx.RequestError)
                or exc.response.status_code >= 500
                or exc.response.status_code in {408, 429}
            )
            if not retryable_status or attempt + 1 >= RESULT_SUBMISSION_ATTEMPTS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(RESULT_SUBMISSION_DELAYS_SECONDS[attempt], remaining))
    if last_error is not None:  # pragma: no cover - loop always returns or raises
        raise last_error


class WorkerTaskContractError(LocalInferenceError):
    """A released worker/server task contract is internally inconsistent."""


def _highlight_context_tokens() -> int:
    """Return the hardware-specific Highlight context within the release ceiling."""

    raw_value = os.getenv(
        "STAR_HIGHLIGHT_CONTEXT_TOKENS",
        str(HIGHLIGHT_COMPARISON_CONTEXT_TOKENS),
    ).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise WorkerTaskContractError("Highlight context override must be an integer") from exc
    if not 4096 <= value <= HIGHLIGHT_COMPARISON_CONTEXT_TOKENS:
        raise WorkerTaskContractError(
            "Highlight context override must be between 4096 and the release ceiling"
        )
    return value


def _highlight_contact_sheet_edge() -> int:
    """Fit Highlight pixels to local accelerator memory without changing candidates."""

    raw_value = os.getenv(
        "STAR_HIGHLIGHT_CONTACT_SHEET_EDGE",
        str(HIGHLIGHT_COMPARISON_CONTACT_SHEET_EDGE),
    ).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise WorkerTaskContractError(
            "Highlight contact-sheet edge override must be an integer"
        ) from exc
    if value not in {1344, 2048, 4096}:
        raise WorkerTaskContractError(
            "Highlight contact-sheet edge override must be 1344, 2048, or 4096"
        )
    return value


def _highlight_worker_envelope() -> tuple[int, int]:
    """Return a locally fitted Highlight envelope and reject unsafe combinations."""

    context_tokens = _highlight_context_tokens()
    contact_sheet_edge = _highlight_contact_sheet_edge()
    # Live RTX 2070 runs proved that Qwen3-VL 8B cannot safely combine a
    # 2048-pixel sheet with an 8192-token KV cache in 8 GiB.  A 1344-pixel
    # sheet produces four roughly 672-pixel cells and fits its visual tokens,
    # prompt, and compact result inside 4096 tokens.  The 4096-pixel quality
    # envelope remains reserved for the 48 GiB Apple at 16384 context.
    if context_tokens < 16_384 and contact_sheet_edge > 1344:
        raise WorkerTaskContractError(
            "Highlight contact-sheet edge exceeds the fitted local context envelope"
        )
    return context_tokens, contact_sheet_edge


def _validated_inference_output(
    schema_type: type[BaseModel],
    value: dict[str, Any],
) -> dict[str, Any]:
    """Convert application-schema drift into a retryable inference failure."""

    try:
        return schema_type.model_validate(value).model_dump(mode="json")
    except PydanticValidationError as exc:
        # Do not log model content or validation inputs.  The provider already
        # validated the public JSON schema; this guard covers application-only
        # validators and keeps an adviser run recoverable instead of surfacing
        # a terminal, unexpected ``ValidationError``.
        raise LocalInferenceSchemaError(
            "local structured inference failed application schema validation"
        ) from exc


def _task_failure_is_retryable(exc: Exception) -> bool:
    """Retry only failures that can change without changing the task contract."""

    return not isinstance(
        exc,
        (
            WorkerTaskContractError,
            LocalInferenceContextWindowError,
            LocalInferenceRequestError,
            LocalInferenceEnvelopeError,
            ValueError,
        ),
    )


def validate_agent_inference_contracts() -> None:
    """Fail a worker build/start before a real photo discovers contract drift."""

    from star.services.pipeline_contract import (
        LEASE_TASK_CONTRACTS,
        validate_pipeline_contract,
    )

    validate_pipeline_contract()
    _highlight_worker_envelope()

    server_tasks = frozenset(LEASE_TASK_CONTRACTS)
    missing_worker_tasks = server_tasks - WORKER_EXECUTABLE_TASK_TYPES
    orphan_worker_tasks = WORKER_EXECUTABLE_TASK_TYPES - server_tasks
    missing_inference_tasks = WORKER_INFERENCE_TASKS - frozenset(ALLOWED_TASKS)
    if missing_worker_tasks or orphan_worker_tasks or missing_inference_tasks:
        raise WorkerTaskContractError("worker task registry does not match the released contract")
    if WORKER_INFERENCE_TASKS & IMAGE_FREE_TASKS:
        raise WorkerTaskContractError("a visual worker inference task was marked image-free")
    senior = TASK_RUNTIME_CONTRACTS["asset.deep_review"]
    if (
        senior.candidate_count != 1
        or senior.context_tokens != SENIOR_REVIEW_CONTEXT_TOKENS
        or senior.max_completion_tokens != SENIOR_REVIEW_MAX_COMPLETION_TOKENS
        or senior.max_media_edge != 1440
    ):
        raise WorkerTaskContractError(
            "senior review worker envelope drifted from the release contract"
        )
    primary = TASK_RUNTIME_CONTRACTS["asset.primary_review"]
    if (
        primary.candidate_count != 1
        or primary.context_tokens != PRIMARY_REVIEW_CONTEXT_TOKENS
        or primary.max_completion_tokens != PRIMARY_VISUAL_MAX_COMPLETION_TOKENS
        or primary.max_media_edge != 2048
    ):
        raise WorkerTaskContractError(
            "primary review worker envelope drifted from the release contract"
        )
    highlight = TASK_RUNTIME_CONTRACTS["group.highlight_compare"]
    if (
        highlight.candidate_count != 4
        or highlight.context_tokens != 16_384
        or highlight.max_media_edge != 2048
        or highlight.contact_sheet_edge != 4096
        or highlight.max_completion_tokens != GROUP_COMPARISON_MAX_COMPLETION_TOKENS
    ):
        raise WorkerTaskContractError(
            "highlight worker envelope drifted from the quality-first release contract"
        )


async def _infer_with_deadline(
    provider: LocalInferenceProvider,
    request: StructuredInferenceRequest,
    *,
    deadline: float | None,
) -> StructuredInferenceResult:
    from star.db.base import utc_now

    started_at = utc_now().isoformat()
    # A task without a compute budget still gets a hang guard: without one,
    # a half-open runtime socket after sleep/wake could pin the lease for
    # the full transport timeout while heartbeats keep renewing it.
    effective_deadline = (
        deadline if deadline is not None else time.monotonic() + DEFAULT_INFERENCE_GUARD_SECONDS
    )
    remaining = effective_deadline - time.monotonic()
    if remaining <= 0:
        raise PhotoComputeBudgetExceeded("photo compute budget was exhausted")
    try:
        result = await asyncio.wait_for(provider.infer(request), timeout=remaining)
        result = dataclasses.replace(
            result, started_at=started_at, completed_at=utc_now().isoformat()
        )
    except TimeoutError as exc:
        raise PhotoComputeBudgetExceeded("photo compute budget was exhausted") from exc
    return result


# Candidate edges the worker fetches and normalizes at; a sheet cell never
# needs more pixels than half the sheet, so the NVIDIA worker's 1344-pixel Highlight
# sheet stops pulling 2048-pixel sources it immediately downsizes.
_GROUP_CANDIDATE_EDGES = (720, 1024, 1280, 1440, 2048)


def _highlight_candidate_edge(contact_sheet_edge: int) -> int:
    """Smallest released candidate edge that still fills one four-up sheet cell."""

    wanted = max(720, contact_sheet_edge // 2)
    for edge in _GROUP_CANDIDATE_EDGES:
        if edge >= wanted:
            return edge
    return HIGHLIGHT_COMPARISON_MAX_MEDIA_EDGE


def _group_max_edge(
    *,
    highlight_comparison: bool,
    series_selection: bool,
    round_kind: str,
    model_key: str,
    contact_sheet_edge: int | None = None,
) -> int:
    if highlight_comparison and model_key == "qwen3-vl:8b-instruct-q4_K_M":
        # The Apple worker's 4096-pixel quality sheet keeps its 2048-pixel sources;
        # the fitted NVIDIA sheet (1344) needs only 720-pixel cells.
        if contact_sheet_edge is None:
            return HIGHLIGHT_COMPARISON_MAX_MEDIA_EDGE
        return min(
            HIGHLIGHT_COMPARISON_MAX_MEDIA_EDGE, _highlight_candidate_edge(contact_sheet_edge)
        )
    if series_selection or (highlight_comparison and round_kind == "four_to_five"):
        return 1280
    if highlight_comparison:
        return 1024
    return 2048


def _sanitized_primary_review(value: object) -> dict[str, Any] | None:
    """Bound the primary star payload handed to the senior review as evidence."""

    if not isinstance(value, dict):
        return None
    candidate = {key: value[key] for key in StarRatingV1.model_fields if key in value}
    try:
        return StarRatingV1.model_validate(candidate).model_dump(mode="json")
    except ValueError:
        return None


def _bounded_context_evidence(value: object, *, max_bytes: int) -> object:
    """Bound untrusted source evidence without logging or silently substituting it."""

    normalized = json.loads(json.dumps(value, sort_keys=True, default=str))

    def encoded_size(candidate: object) -> int:
        return len(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    if encoded_size(normalized) <= max_bytes:
        return normalized
    digest = _digest_json(normalized)
    marker: dict[str, object] = {
        "_star_truncated": True,
        "source_digest": digest,
    }
    if isinstance(normalized, dict):
        bounded: dict[str, object] = dict(marker)
        for raw_key, raw_item in sorted(normalized.items(), key=lambda item: str(item[0])):
            key = str(raw_key)[:128]
            item = _bounded_context_evidence(
                raw_item,
                max_bytes=max(256, min(2_048, max_bytes // 3)),
            )
            candidate = {**bounded, key: item}
            if encoded_size(candidate) > max_bytes:
                continue
            bounded[key] = item
        return bounded
    if isinstance(normalized, list):
        bounded_items: list[object] = []
        for raw_item in normalized:
            item = _bounded_context_evidence(
                raw_item,
                max_bytes=max(256, min(2_048, max_bytes // 3)),
            )
            candidate = {**marker, "items": [*bounded_items, item]}
            if encoded_size(candidate) > max_bytes:
                break
            bounded_items.append(item)
        return {**marker, "items": bounded_items}
    text = str(normalized)
    retained_characters = max(0, max_bytes - encoded_size(marker) - 64)
    return {**marker, "text_prefix": text[:retained_characters]}


# Manifest inference settings (v209). The coordinator embeds the owner's
# per-stage settings in every verified manifest; the worker applies them as
# request overrides. A setting that equals the released baseline is not turned
# into an override, so a manifest carrying the defaults (or none at all)
# produces a request byte-identical to the pre-v209 worker, seed included.
OWNER_CALIBRATION_MARKER = "\n\nLearned from the owner's corrections:"


def _manifest_inference_settings(
    manifest: dict[str, Any] | None,
    *,
    role: InferenceStage,
) -> InferenceStageSettings | None:
    """Read one stage's settings from a manifest; malformed or absent means none."""

    if manifest is None:
        return None
    raw = manifest.get("inference_settings")
    if not isinstance(raw, dict):
        return None
    stage = raw.get(role)
    if not isinstance(stage, dict):
        return None
    try:
        return InferenceStageSettings.model_validate(stage)
    except ValueError:
        logger.warning(
            "manifest inference settings ignored",
            extra={"role": role, "error_class": "InferenceSettingsInvalid"},
        )
        return None


def _strip_owner_calibration(text: str | None) -> str | None:
    """Drop the derived correction block the coordinator appends to the owner taste."""

    if text is None:
        return None
    marker = text.find(OWNER_CALIBRATION_MARKER)
    return text if marker < 0 else text[:marker].rstrip()


def _settings_reasoning_effort(
    settings: InferenceStageSettings | None,
    default: str,
) -> str:
    effort = default if settings is None else settings.reasoning_effort
    # Never above medium, whatever the manifest says.
    return effort if effort in {"off", "low", "medium"} else "medium"


def _apply_stage_settings(
    request: StructuredInferenceRequest,
    settings: InferenceStageSettings | None,
) -> StructuredInferenceRequest:
    """Turn owner stage settings into request overrides where they differ from the baseline."""

    if settings is None:
        return request
    # Mirror the providers' thinking decision so the baseline profile is the
    # one the runtime would pick without any override.
    is_thinking = request.reasoning_effort != "off" and not request.model_key.startswith(
        "ministral-3:"
    )
    baseline = sampling_profile_for(
        dataclasses.replace(
            request,
            temperature_override=None,
            top_p_override=None,
            top_k_override=None,
            min_p_override=None,
            repeat_penalty_override=None,
            presence_penalty_override=None,
        ),
        is_thinking=is_thinking,
    )
    sampling = settings.sampling
    if sampling.temperature != baseline.temperature:
        request = dataclasses.replace(request, temperature_override=sampling.temperature)
    if sampling.top_p != baseline.top_p:
        request = dataclasses.replace(request, top_p_override=sampling.top_p)
    if sampling.top_k != baseline.top_k:
        request = dataclasses.replace(request, top_k_override=sampling.top_k)
    if sampling.min_p != baseline.min_p:
        request = dataclasses.replace(request, min_p_override=sampling.min_p)
    if sampling.repeat_penalty != baseline.repeat_penalty:
        request = dataclasses.replace(request, repeat_penalty_override=sampling.repeat_penalty)
    if sampling.presence_penalty != baseline.presence_penalty:
        request = dataclasses.replace(request, presence_penalty_override=sampling.presence_penalty)
    return request


def _structured_request_digest(request: StructuredInferenceRequest) -> str:
    """Hash every material input to one reusable structured-inference pass."""

    return _digest_json(
        {
            "model_key": request.model_key,
            "task": request.task,
            "resolved_instruction": (request.instruction_override or ALLOWED_TASKS[request.task]),
            "instruction_source": request.instruction_source,
            "rating_scale_override": request.rating_scale_override,
            "owner_taste_override": request.owner_taste_override,
            "system_prompt_override": request.system_prompt_override,
            "schema": request.schema,
            "context": request.context,
            "reasoning_effort": request.reasoning_effort,
            "max_completion_tokens": request.max_completion_tokens,
            "visual_token_budget": request.visual_token_budget,
            "context_tokens_override": request.context_tokens_override,
            "seed_override": request.seed_override,
            "temperature_override": request.temperature_override,
            "top_p_override": request.top_p_override,
            "top_k_override": request.top_k_override,
            "min_p_override": request.min_p_override,
            "repeat_penalty_override": request.repeat_penalty_override,
            "media": [
                {
                    "digest": hashlib.sha256(item.content).hexdigest(),
                    "media_type": item.media_type,
                    "source": item.source,
                    "asset_reference": item.asset_reference,
                    "processing_mode": item.processing_mode,
                    "orientation_policy": item.orientation_policy,
                    "max_edge": item.max_edge,
                    "encoding_quality": item.encoding_quality,
                }
                for item in request.media
            ],
        }
    )


async def _process_vlm_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider,
) -> None:
    task_id = str(task["task_id"])
    lease_id = str(task["lease_id"])
    target = task["target_ref"]
    raw_budget = target.get("compute_budget_ms")
    budget_ms = (
        int(raw_budget)
        if isinstance(raw_budget, (int, float))
        and not isinstance(raw_budget, bool)
        and raw_budget > 0
        else None
    )
    highlight_comparison = task["task_type"] == "group.highlight_compare"
    series_selection = task["task_type"] == "group.series_select"
    highlight_context_tokens: int | None = None
    highlight_contact_sheet_edge: int | None = None
    if highlight_comparison:
        highlight_context_tokens, highlight_contact_sheet_edge = _highlight_worker_envelope()
    asset_ids = _validated_group_asset_ids(
        target.get("asset_ids", []),
        highlight_comparison=highlight_comparison,
    )
    if highlight_comparison:
        runtime_contract = HIGHLIGHT_COMPARISON_RUNTIME
        expected_envelope: dict[str, object] = {
            "context_tokens": runtime_contract.context_tokens,
            "max_completion_tokens": runtime_contract.max_completion_tokens,
            "max_media_edge": runtime_contract.max_media_edge,
            "contact_sheet_edge": runtime_contract.contact_sheet_edge,
        }
        mismatched = [
            key for key, expected in expected_envelope.items() if target.get(key) != expected
        ]
        if mismatched:
            raise WorkerTaskContractError(
                "highlight runtime envelope mismatch: " + ", ".join(mismatched)
            )
        prompt_version = str(target.get("prompt_version") or "")
        accepted_prompt_versions = {
            "",
            HIGHLIGHT_PROMPT_VERSION,
            "highlight-compare-content-first-family-memory-v8",
            "highlight-compare-holistic-family-memory-v7",
            "highlight-compare-family-memory-v6",
            "highlight-compare-high-resolution-v5",
        }
        if prompt_version not in accepted_prompt_versions:
            raise WorkerTaskContractError("highlight prompt version is unsupported")
    round_kind = str(target.get("round_kind", "normal"))
    manifest = await _execution_manifest(client, task)
    prompt_version = str(target.get("prompt_version") or "")
    model_key = str(target["model_key"])
    group_settings = _manifest_inference_settings(manifest, role="group")
    max_edge = _group_max_edge(
        highlight_comparison=highlight_comparison,
        series_selection=series_selection,
        round_kind=round_kind,
        model_key=model_key,
        contact_sheet_edge=highlight_contact_sheet_edge,
    )
    if group_settings is not None and group_settings.max_media_edge is not None:
        # Owner inference settings may shrink the fetched candidate edge; they
        # never enlarge it past the released envelope for the group.
        max_edge = min(max_edge, group_settings.max_media_edge)
    media: list[LocalMediaInput] = []
    media_provenance: list[dict[str, Any]] = []
    cache_tasks: list[asyncio.Task[None]] = []
    fetches: list[asyncio.Task[tuple[httpx.Response, LocalMediaInput]]] = []
    try:
        async with asyncio.TaskGroup() as fetch_group:
            fetches = [
                fetch_group.create_task(
                    _fetch_prepared_media(
                        client,
                        path=f"/api/v1/workers/tasks/{task_id}/media/{asset_id}",
                        lease_id=lease_id,
                        max_edge=max_edge,
                    )
                )
                for asset_id in asset_ids
            ]
    except BaseExceptionGroup as fetch_failure:
        raise _leaf_task_failure(fetch_failure) from None
    for index, (asset_id, fetch) in enumerate(zip(asset_ids, fetches, strict=True), start=1):
        response, prepared = fetch.result()
        cache_tasks.append(
            asyncio.create_task(
                _cache_normalized_media(client, task, asset_id=str(asset_id), media=prepared)
            )
        )
        media.append(
            LocalMediaInput(
                content=prepared.content,
                media_type=prepared.media_type,
                source=prepared.source,
                asset_reference=f"candidate-{index}",
            )
        )
        with Image.open(io.BytesIO(prepared.content)) as normalized:
            normalized_width, normalized_height = normalized.size
        media_provenance.append(
            _media_provenance(
                response,
                asset_reference=f"candidate-{index}",
                processing_content=prepared.content,
                processing_media_type=prepared.media_type,
                processing_width=normalized_width,
                processing_height=normalized_height,
                processing_mode=prepared.processing_mode,
                orientation_policy=prepared.orientation_policy,
                max_edge=max_edge,
                encoding_quality=prepared.encoding_quality,
            )
        )
    comparison_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ranking"],
        "properties": {
            "ranking": {
                "type": "array",
                "minItems": len(asset_ids),
                "maxItems": len(asset_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_index", "quality"],
                    "properties": {
                        "candidate_index": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": len(asset_ids),
                        },
                        "quality": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                },
            },
        },
    }
    contact_sheet_edge = highlight_contact_sheet_edge if highlight_comparison else None
    inference_media = (
        [_build_group_contact_sheet(media, sheet_edge=contact_sheet_edge)]
        if contact_sheet_edge is not None
        else media
    )
    # Start the compute deadline immediately before local inference. Media fetch,
    # EXIF normalization, and prompt-contract reads are worker preparation, not
    # model compute. Provider inference still includes a cold model load.
    owner_taste_override, _owner_taste_source = await _coordinator_prompt(
        client,
        OWNER_TASTE_PROMPT_KEY,
        revision=_pinned_prompt_revisions(target).get(OWNER_TASTE_PROMPT_KEY),
        manifest=manifest,
    )
    if group_settings is not None and not group_settings.include_calibration:
        owner_taste_override = _strip_owner_calibration(owner_taste_override)
    inference_deadline = time.monotonic() + budget_ms / 1000 if budget_ms is not None else None
    group_reasoning_effort = _settings_reasoning_effort(group_settings, "off")
    request = StructuredInferenceRequest(
        model_key=str(target["model_key"]),
        task="group_compare",
        owner_taste_override=owner_taste_override,
        reasoning_effort=(
            "medium"
            if group_reasoning_effort == "medium"
            else "low"
            if group_reasoning_effort == "low"
            else "off"
        ),
        media=tuple(inference_media),
        schema=comparison_schema,
        context={
            "group_type": target.get("group_type"),
            "candidate_count": len(asset_ids),
            "media_layout": (
                "single_labelled_contact_sheet" if highlight_comparison else "separate_images"
            ),
            "candidate_context": target.get("candidate_context", []),
            "comparison_contract": (
                {
                    "purpose": "blind collection-relative private-archive highlight ranking",
                    "round_kind": round_kind,
                    "instructions": [
                        "Return only the candidates ordered from best overall photo to weakest.",
                        "Use each visible number badge as candidate_index and ignore input order.",
                        "Give each candidate a relative quality integer from 0 to 100.",
                        "Judge as a private family-memory archive: prioritize readable people, "
                        "story, humor, fun, personality, interaction, color, atmosphere, "
                        "travel, change over time, and documentary importance.",
                        "Use supplied Immich-attributed family context as collection context, "
                        "never as a visual identity claim. A readable shared moment involving "
                        "two or more core family members, the whole family, or children "
                        "growing up can outrank a cleaner but less meaningful frame.",
                        "When candidates are otherwise close, prefer a distinctive event, "
                        "family grouping, stage of life, setting, or story over a similar "
                        "frame. Never promote names alone.",
                        "Technical quality separates otherwise comparable memories and matters "
                        "strongly only when it obscures the central content.",
                        "A stronger visible content path with moderate technical flaws can "
                        "outrank a technically cleaner candidate with a weaker content path.",
                        "Recognize coherent selective focus, foreground or background bokeh, "
                        "and layered depth. The nearest person or object need not be the "
                        "important subject when a sharper distant subject anchors the frame.",
                        "Compare each photograph's strongest surviving path against "
                        "its largest material flaw; count the same underlying flaw only once.",
                        "Keep incidental edge fragments and background bystanders secondary "
                        "unless they materially damage the central photograph.",
                        "People, pets, landscapes, travel, still life, and abstract "
                        "photographs can all win.",
                        "Lower accidental closed eyes, blocked faces, failed timing, awkward "
                        "crops, and clearly weaker burst frames.",
                        "Do not infer identity, relationship, emotion, intent, or hidden "
                        "importance. Do not explain, describe, classify, or assign stars.",
                    ]
                    if prompt_version
                    in {
                        HIGHLIGHT_PROMPT_VERSION,
                        "highlight-compare-holistic-family-memory-v7",
                        "highlight-compare-content-first-family-memory-v8",
                    }
                    else [
                        "Return only the candidates ordered from best overall photo to weakest.",
                        "Use each visible number badge as candidate_index and ignore input order.",
                        "Give each candidate a relative quality integer from 0 to 100.",
                        "Judge the whole photograph: moment, expression, timing, subject "
                        "visibility, composition, light, technical condition, atmosphere, "
                        "visual interest, and lasting private-archive value.",
                        "People, pets, landscapes, travel, still life, and abstract "
                        "photographs can all win.",
                        "Lower accidental closed eyes, blocked faces, failed timing, awkward "
                        "crops, and clearly weaker burst frames.",
                        "Do not explain, describe, classify, or assign stars.",
                    ],
                }
                if highlight_comparison
                else {
                    "purpose": "blind near-duplicate series representative selection",
                    "instructions": [
                        "Rank every candidate from strongest frame to weakest.",
                        "Use each visible number badge as candidate_index and ignore input order.",
                        "Choose the one frame that best preserves the visible family memory, "
                        "story, humor, interaction, growth, atmosphere, and photographic intent.",
                        "Use focus-plane choice, layered depth, selective focus, bokeh, timing, "
                        "expression, light, color, framing, and recoverability as visible "
                        "evidence.",
                        "Prefer readable eyes and faces when they matter, but do not punish a "
                        "deliberate distant focus plane or a stronger candid/documentary moment.",
                        "When several frames tell the same story, keep only the strongest "
                        "representative; preserve a second only when it records a materially "
                        "different visible moment or story.",
                        "Return only relative quality integers. Do not assign stars or "
                        "infer identity, relationship, hidden emotion, or intent.",
                    ],
                }
                if series_selection
                else None
            ),
            "limitations": [
                *sorted({item.source for item in media}),
                "identity_and_emotional_value_may_be_uncertain",
            ],
        },
        max_completion_tokens=(
            # The owner's group setting governs the 256-token comparison tasks.
            group_settings.max_completion_tokens
            if group_settings is not None
            else GROUP_COMPARISON_MAX_COMPLETION_TOKENS
        ),
        context_tokens_override=(
            highlight_context_tokens
            if highlight_comparison
            else int(target.get("context_tokens", 16_384))
        ),
    )
    request = _apply_stage_settings(request, group_settings)
    try:
        result = await _infer_with_deadline(provider, request, deadline=inference_deadline)
        inference_runs = [
            _inference_accounting(
                result,
                phase=("highlight_comparison" if highlight_comparison else "series_selection"),
                input_digest=_digest_json(
                    {
                        "task": "group_compare",
                        "round_kind": round_kind,
                        "media_digests": [
                            hashlib.sha256(item.content).hexdigest() for item in inference_media
                        ],
                    }
                ),
                image_count=len(inference_media),
                prompt_version=str(
                    target.get("prompt_version")
                    or ("highlight-league-v1" if highlight_comparison else "series-selector-v1")
                ),
                schema_version="group-ranking-v1",
            )
        ]
        await _drain_cache_tasks(cache_tasks)
        await _renew_lease(client, task, stage="submitting")
        endpoint = (
            "complete-highlight-comparison" if highlight_comparison else "complete-series-selection"
        )
        await _submit_completed_result(
            client,
            task,
            f"/api/v1/workers/tasks/{task_id}/{endpoint}",
            {
                **result.value,
                "model_reported_version": result.model_version,
                "runtime": result.runtime,
                "duration_ms": result.duration_ms,
                "media_provenance": media_provenance,
                "inference_runs": inference_runs,
            },
        )
    except BaseException:
        await _drain_cache_tasks(cache_tasks, cancel=True)
        raise


def _pinned_prompt_revisions(target: dict[str, Any]) -> dict[str, int]:
    """Return the immutable prompt revisions pinned on a task, if any."""

    raw = target.get("prompt_revisions", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    }


# Pinned prompt revisions are immutable, so their text is cacheable for the
# worker process lifetime. Unpinned lookups stay live so an owner edit reaches
# the very next request.
_PINNED_PROMPT_CACHE_LIMIT = 64
_pinned_prompt_cache: dict[tuple[str, int], tuple[str | None, str]] = {}
_manifest_cache: dict[str, dict[str, Any]] = {}


async def _execution_manifest(client: httpx.AsyncClient, task: dict[str, Any]) -> dict[str, Any]:
    digest = task.get("target_ref", {}).get("execution_manifest_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise LocalInferenceTransportError("verified execution manifest is required")
    if digest in _manifest_cache:
        cached = _manifest_cache[digest]
        if cached.get("task_type") != task.get("task_type") or cached.get("model_key") != task.get(
            "target_ref", {}
        ).get("model_key"):
            raise LocalInferenceTransportError("execution manifest task identity mismatch")
        return cached
    try:
        response = await client.get(f"/api/v1/workers/execution-manifests/{digest}", timeout=15.0)
        response.raise_for_status()
        value = response.json()
        payload = value["payload"]
        if (
            not isinstance(payload, dict)
            or value.get("digest") != digest
            or _digest_json(payload) != digest
        ):
            raise ValueError("manifest mismatch")
        if payload.get("task_type") != task.get("task_type") or payload.get(
            "model_key"
        ) != task.get("target_ref", {}).get("model_key"):
            raise ValueError("manifest identity mismatch")
        for component in payload["components"].values():
            instruction = component["instruction"]
            if (
                not isinstance(instruction, str)
                or hashlib.sha256(instruction.encode()).hexdigest() != component["digest"]
            ):
                raise ValueError("component digest mismatch")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LocalInferenceTransportError("verified execution manifest unavailable") from exc
    if len(_manifest_cache) >= _PINNED_PROMPT_CACHE_LIMIT:
        _manifest_cache.pop(next(iter(_manifest_cache)))
    _manifest_cache[digest] = payload
    return payload


async def _coordinator_prompt(
    client: httpx.AsyncClient,
    inference_task: str,
    *,
    revision: int | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Fetch the coordinator's prompt text for one inference task.

    A missing or malformed coordinator response is retryable. A production task
    must never silently switch to a different instruction set.
    """

    if manifest is not None:
        component = manifest.get("components", {}).get(inference_task)
        if not isinstance(component, dict) or (
            revision is not None and component.get("revision") != revision
        ):
            raise LocalInferenceTransportError("execution prompt revision mismatch")
        return str(component["instruction"]), f"coordinator_revision_{component['revision']}"
    cache_key = (inference_task, revision) if revision is not None else None
    if cache_key is not None and cache_key in _pinned_prompt_cache:
        return _pinned_prompt_cache[cache_key]
    try:
        response = await client.get(
            f"/api/v1/workers/prompts/{inference_task}",
            params={"version": revision} if revision is not None else None,
        )
    except httpx.HTTPError as exc:
        raise LocalInferenceTransportError("coordinator prompt unavailable") from exc
    if response.status_code != 200:
        raise LocalInferenceTransportError("coordinator prompt unavailable")
    try:
        payload = response.json()
        instruction = payload["instruction"]
        returned_revision = payload["revision"]
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or type(returned_revision) is not int
            or returned_revision <= 0
            or (revision is not None and returned_revision != revision)
            or hashlib.sha256(instruction.encode()).hexdigest() != payload["instruction_digest"]
        ):
            raise ValueError("invalid prompt response")
    except (ValueError, KeyError, TypeError) as exc:
        raise LocalInferenceTransportError("coordinator prompt verification failed") from exc
    result = (instruction, f"coordinator_revision_{returned_revision}")
    if cache_key is not None and len(_pinned_prompt_cache) < _PINNED_PROMPT_CACHE_LIMIT:
        _pinned_prompt_cache[cache_key] = result
    return result


async def _process_asset_vlm_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider,
) -> None:
    """Rate one photo: the NVIDIA worker primary review or the Apple worker senior review.

    Both are one visual request that returns the compact star-rating contract.
    The senior review additionally receives the bounded primary review as
    untrusted evidence and is allowed to think before answering.
    """

    task_id = str(task["task_id"])
    lease_id = str(task["lease_id"])
    target = task["target_ref"]
    task_type = str(task.get("task_type"))
    if task_type not in {"asset.primary_review", "asset.deep_review"}:
        raise WorkerTaskContractError("unsupported individual review task type")
    is_senior = task_type == "asset.deep_review"
    safe_primary_review = _sanitized_primary_review(target.get("primary_review"))
    if is_senior and safe_primary_review is None:
        raise WorkerTaskContractError("senior review is missing the primary review")
    raw_compute_budget_ms = target.get("compute_budget_ms")
    compute_budget_ms = (
        int(raw_compute_budget_ms)
        if isinstance(raw_compute_budget_ms, (int, float))
        and not isinstance(raw_compute_budget_ms, bool)
        and raw_compute_budget_ms > 0
        else None
    )
    # The verified manifest is read before the media so the owner's stage
    # settings can decide the fetch edge; it is cached per digest, so this
    # costs nothing on the hot path.
    manifest = await _execution_manifest(client, task)
    stage_settings = _manifest_inference_settings(
        manifest, role="advanced" if is_senior else "primary"
    )
    max_edge = _target_media_edge(target)
    if stage_settings is not None and target.get("runtime_envelope") != "fallback":
        # A declared fallback envelope (after a context/memory failure) keeps
        # priority over the owner's preferred edge.
        max_edge = _target_media_edge({"max_media_edge": stage_settings.max_media_edge})
    response, media = await _fetch_prepared_media(
        client,
        path=f"/api/v1/workers/tasks/{task_id}/media",
        lease_id=lease_id,
        max_edge=max_edge,
        asset_id=_task_asset_id(task),
    )
    with Image.open(io.BytesIO(media.content)) as normalized:
        normalized_width, normalized_height = normalized.size
    cache_tasks: list[asyncio.Task[None]] = []
    if target.get("asset_id") is not None:
        cache_tasks.append(
            asyncio.create_task(
                _cache_normalized_media(
                    client,
                    task,
                    asset_id=str(target["asset_id"]),
                    media=media,
                )
            )
        )
    try:
        media_provenance = _media_provenance(
            response,
            asset_reference="asset",
            processing_content=media.content,
            processing_media_type=media.media_type,
            processing_width=normalized_width,
            processing_height=normalized_height,
            processing_mode=media.processing_mode,
            orientation_policy=media.orientation_policy,
            max_edge=max_edge,
            encoding_quality=media.encoding_quality,
        )
        prompt_revisions = _pinned_prompt_revisions(target)
        prompt_version = str(target.get("prompt_version") or "senior-review-v1")
        inference_task = f"photo_deep_review:{prompt_version}" if is_senior else "primary_review"
        system_prompt_key = APPLE_SYSTEM_PROMPT_KEY if is_senior else NVIDIA_SYSTEM_PROMPT_KEY
        (
            (instruction_override, instruction_source),
            (rating_scale_override, _scale_source),
            (owner_taste_override, _taste_source),
            (system_prompt_override, _system_prompt_source),
        ) = await asyncio.gather(
            _coordinator_prompt(
                client,
                inference_task,
                revision=prompt_revisions.get(inference_task),
                manifest=manifest,
            ),
            _coordinator_prompt(
                client,
                RATING_SCALE_PROMPT_KEY,
                revision=prompt_revisions.get(RATING_SCALE_PROMPT_KEY),
                manifest=manifest,
            ),
            _coordinator_prompt(
                client,
                OWNER_TASTE_PROMPT_KEY,
                revision=prompt_revisions.get(OWNER_TASTE_PROMPT_KEY),
                manifest=manifest,
            ),
            _coordinator_prompt(
                client,
                system_prompt_key,
                revision=prompt_revisions.get(system_prompt_key),
                manifest=manifest,
            ),
        )
        effective_system_prompt = system_prompt_override or MODEL_SYSTEM_PROMPTS[system_prompt_key]
        if stage_settings is not None and not stage_settings.include_calibration:
            owner_taste_override = _strip_owner_calibration(owner_taste_override)
        stage_reasoning_effort = _settings_reasoning_effort(stage_settings, "off")
        # One deadline for the complete model task. It starts only when
        # inference begins and includes any cold model load.
        inference_deadline = (
            time.monotonic() + compute_budget_ms / 1000 if compute_budget_ms is not None else None
        )
        visual_context: dict[str, object] = {
            "review_profile": "senior_review" if is_senior else "primary_review",
            "capture_facts": target.get("capture_facts", {}),
            "series_facts": target.get("series_facts", {}),
            "trusted_family_context": target.get("trusted_family_context", {}),
            "limitations": [
                media.source,
                (
                    "family_identity_is_immich_attributed_not_visually_inferred"
                    if target.get("trusted_family_context")
                    else "family_importance_and_identity_may_be_unknown"
                ),
            ],
        }
        if is_senior:
            visual_context["primary_review"] = safe_primary_review
            visual_context["escalation_reasons"] = target.get("escalation_reasons", [])
        visual_request = StructuredInferenceRequest(
            model_key=str(target["model_key"]),
            task=inference_task,
            instruction_override=instruction_override,
            instruction_source=instruction_source,
            rating_scale_override=rating_scale_override,
            owner_taste_override=owner_taste_override,
            system_prompt_override=effective_system_prompt,
            media=(media,),
            schema=ASSET_VLM_SCHEMA,
            context=visual_context,
            # The senior model may think before answering; the 8B primary
            # answers directly. Neither is capped below what the runtime
            # allows, and the owner's stage settings may move either within
            # the bounded range (never above medium).
            reasoning_effort=(
                "medium"
                if stage_reasoning_effort == "medium"
                else "low"
                if stage_reasoning_effort == "low"
                else "off"
            ),
            max_completion_tokens=(
                stage_settings.max_completion_tokens
                if stage_settings is not None
                else SENIOR_REVIEW_MAX_COMPLETION_TOKENS
                if is_senior
                else PRIMARY_VISUAL_MAX_COMPLETION_TOKENS
            ),
            context_tokens_override=_leased_context_tokens(
                target,
                default=(
                    SENIOR_REVIEW_CONTEXT_TOKENS if is_senior else PRIMARY_REVIEW_CONTEXT_TOKENS
                ),
            ),
        )
        visual_request = _apply_stage_settings(visual_request, stage_settings)
        visual_request = _apply_retry_sampling_seed(visual_request, target)
        media_digest = hashlib.sha256(media.content).hexdigest()
        result = await _infer_with_deadline(
            provider,
            visual_request,
            deadline=inference_deadline,
        )
        inference_runs = [
            _inference_accounting(
                result,
                phase="senior_review" if is_senior else "primary_visual",
                input_digest=_digest_json(
                    {
                        "task": inference_task,
                        "prompt_version": prompt_version if is_senior else instruction_source,
                        "media_digest": media_digest,
                    }
                ),
                image_count=1,
                prompt_version=prompt_version if is_senior else instruction_source,
                schema_version=STAR_RATING_SCHEMA_VERSION,
            )
        ]
        await _drain_cache_tasks(cache_tasks)
        await _submit_completed_result(
            client,
            task,
            f"/api/v1/workers/tasks/{task_id}/complete-asset-vlm",
            {
                **result.value,
                "model_reported_version": result.model_version,
                "runtime": result.runtime,
                "duration_ms": result.duration_ms,
                "media_provenance": media_provenance,
                "inference_runs": inference_runs,
            },
        )
    except BaseException:
        await _drain_cache_tasks(cache_tasks, cancel=True)
        raise


async def _process_model_warmup_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider,
) -> None:
    target = task["target_ref"]
    model_key = str(target["model_key"])
    started = time.monotonic()
    await provider.warmup(model_key)
    inventory = await provider.inventory()
    model = next((item for item in inventory if item.model_key == model_key), None)
    await _submit_completed_result(
        client,
        task,
        f"/api/v1/workers/tasks/{task['task_id']}/complete-warmup",
        {
            "model_reported_version": model.version if model is not None else model_key,
            "runtime": model.runtime if model is not None else "local-model-runtime",
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        },
    )


async def _process_media_stage_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
) -> None:
    """Pre-fetch and normalize both review derivatives into the coordinator cache.

    Staging is administrative evidence preparation: it burns CPU while the
    Immich media disks are awake so later model attempts read the NVMe cache
    instead of waking them again.
    """

    task_id = str(task["task_id"])
    lease_id = str(task["lease_id"])
    asset_id = str(task["target_ref"]["asset_id"])
    started = time.monotonic()
    variants_stored: list[str] = []
    bytes_stored = 0
    for cache_variant, source_variant, max_edge in (
        ("thumbnail", "thumbnail", 720),
        ("preview", "fullsize", 2048),
    ):
        response = await _fetch_media_response(
            client,
            path=f"/api/v1/workers/tasks/{task_id}/media",
            lease_id=lease_id,
            asset_id=asset_id,
            params={"variant": source_variant},
        )
        if response.headers.get("x-star-cache") == "normalized-jpeg-q80":
            variants_stored.append(cache_variant)
            continue
        input_variant = response.headers.get("x-star-input-variant", "preview")
        prepared = await _prepared_media_input(
            response.content, input_variant, max_edge, asset_id=asset_id
        )
        stored = await client.post(
            f"/api/v1/workers/tasks/{task_id}/normalized-media/{asset_id}",
            params={"variant": cache_variant},
            headers={
                "X-Star-Lease": lease_id,
                "Content-Type": "image/jpeg",
                "X-Star-Encoding-Quality": "80",
            },
            content=prepared.content,
            timeout=30.0,
        )
        stored.raise_for_status()
        variants_stored.append(cache_variant)
        bytes_stored += len(prepared.content)
    await _submit_completed_result(
        client,
        task,
        f"/api/v1/workers/tasks/{task_id}/complete-media-stage",
        {
            "variants_stored": variants_stored,
            "bytes_stored": bytes_stored,
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        },
    )


def _embedding_auth_headers() -> dict[str, str]:
    token = os.getenv("STAR_EMBEDDING_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _process_library_review_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider,
) -> None:
    """Read the library's own numbers and answer with findings and proposals.

    Text only: the evidence pack arrives inline in ``target_ref`` and holds
    counts, never photographs, filenames or prompts. The answer is bounded by a
    schema so it can be acted on rather than interpreted, and it changes nothing
    on its own.
    """

    task_id = str(task["task_id"])
    target = dict(task.get("target_ref") or {})
    evidence = target.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        raise WorkerTaskContractError("library review task carries no evidence pack")
    model_key = str(target.get("model_key") or LIBRARY_REVIEW_MODEL_KEY)
    request = StructuredInferenceRequest(
        model_key=model_key,
        task="library_review",
        media=(),
        schema=LIBRARY_REVIEW_SCHEMA,
        context={"review_profile": "library_review", "evidence": evidence},
        # Medium is the ceiling anywhere in Star, and this is the one task where
        # thinking earns its cost: it is comparing tonight against earlier
        # nights rather than describing a single photograph.
        reasoning_effort="medium",
        max_completion_tokens=LIBRARY_REVIEW_MAX_COMPLETION_TOKENS,
        context_tokens_override=LIBRARY_REVIEW_CONTEXT_TOKENS,
    )
    deadline = time.monotonic() + LIBRARY_REVIEW_COMPUTE_BUDGET_MS / 1000
    result = await _infer_with_deadline(provider, request, deadline=deadline)
    await _renew_lease(client, task, stage="submitting")
    response = await client.post(
        f"/api/v1/workers/tasks/{task_id}/complete-library-review",
        params={"lease_id": str(task["lease_id"])},
        json={
            "note_id": str(target.get("note_id")),
            "model_key": model_key,
            "value": result.value,
            "inference_runs": [
                _inference_accounting(
                    result,
                    phase="library_review",
                    input_digest=_digest_json({"evidence": evidence}),
                    image_count=0,
                    prompt_version="library-review-v1",
                    schema_version="library-review-v1",
                )
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()


async def _execute_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider | None,
) -> None:
    task_id = str(task["task_id"])
    lease_id = str(task["lease_id"])
    if task["task_type"] == "asset.semantic_embedding":
        execution_started = time.monotonic()
        response = await _fetch_media_response(
            client,
            path="/api/v1/workers/tasks/" + task_id + "/media",
            lease_id=lease_id,
            asset_id=_task_asset_id(task),
        )
        with Image.open(io.BytesIO(response.content)) as source_image:
            source_width, source_height = source_image.size
        source_media_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        media_provenance = _media_provenance(
            response,
            asset_reference="asset",
            processing_content=response.content,
            processing_media_type=source_media_type,
            processing_width=source_width,
            processing_height=source_height,
            processing_mode="source_derivative",
            orientation_policy="provider_native",
        )
        runtime_url = os.getenv(
            "STAR_SEMANTIC_EMBEDDING_URL",
            "http://embedding-runtime:8095",
        )
        parsed_runtime = httpx.URL(runtime_url)
        if parsed_runtime.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
            "embedding-runtime",
        }:
            raise RuntimeError("semantic embedding runtime host is not locally allowlisted")
        async with httpx.AsyncClient(
            base_url=runtime_url,
            follow_redirects=False,
            timeout=httpx.Timeout(180.0),
        ) as embedding_client:
            embedded = await embedding_client.post(
                "/embed",
                content=response.content,
                headers={
                    "Content-Type": response.headers.get("content-type", "image/jpeg"),
                    **_embedding_auth_headers(),
                },
            )
            if embedded.is_redirect:
                raise RuntimeError("semantic embedding runtime redirect rejected")
            embedded.raise_for_status()
            result = embedded.json()
        # Layout fingerprints for burst/series grouping travel with the
        # similarity task since v209 round 3. An undecodable image simply
        # ships without them; the embedding itself still lands.
        fingerprints: dict[str, str] | None
        try:
            fingerprints = compute_visual_fingerprints(response.content).payload()
        except UnsafeImageError:
            fingerprints = None
        await _submit_completed_result(
            client,
            task,
            "/api/v1/workers/tasks/" + task_id + "/complete-semantic-embedding",
            {
                **result,
                "input_digest": hashlib.sha256(response.content).hexdigest(),
                "duration_ms": max(0, int((time.monotonic() - execution_started) * 1000)),
                "media_provenance": media_provenance,
                "fingerprints": fingerprints,
            },
        )
        return
    lease_id = str(task["lease_id"])
    if task["task_type"] == "worker.model_warmup":
        if provider is None:
            await _submit_failure(client, task, "ModelProviderUnavailable", retryable=True)
            return
        await _process_model_warmup_task(client, task, provider)
        return
    if task["task_type"] == "asset.media_stage":
        await _process_media_stage_task(client, task)
        return
    if task["task_type"] in GROUP_VLM_TASK_TYPES:
        if provider is None:
            await _submit_failure(client, task, "ModelProviderUnavailable", retryable=True)
            return
        await _process_vlm_task(client, task, provider)
        return
    if task["task_type"] in ASSET_VLM_TASK_TYPES:
        if provider is None:
            await _submit_failure(client, task, "ModelProviderUnavailable", retryable=True)
            return
        await _process_asset_vlm_task(client, task, provider)
        return
    if task["task_type"] == LIBRARY_REVIEW_TASK_TYPE:
        if provider is None:
            await _submit_failure(client, task, "ModelProviderUnavailable", retryable=True)
            return
        await _process_library_review_task(client, task, provider)
        return
    await _submit_failure(client, task, "UnsupportedTaskType", retryable=False)


async def _process_task_inner(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider | None,
) -> None:
    task_id = str(task["task_id"])
    finished = asyncio.Event()
    execution = asyncio.create_task(_execute_task(client, task, provider))
    heartbeat = asyncio.create_task(_maintain_lease(client, task, finished))
    try:
        done, _ = await asyncio.wait({execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # asyncio.wait does not propagate cancellation to its children; without
        # this, a cancelled lane would leave execution and the lease heartbeat
        # running detached.
        for child in (execution, heartbeat):
            child.cancel()
        for child in (execution, heartbeat):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await child
        raise
    if heartbeat in done:
        try:
            await heartbeat
        except TaskCancelledError:
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            with contextlib.suppress(httpx.HTTPError):
                await _submit_failure(client, task, "TaskCancelled", retryable=False)
            return
        except LeaseLostError:
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            # If renewal failed because the coordinator was briefly unreachable, the
            # server may still hold the lease. Return it when possible rather
            # than leaving the next poll blocked at capacity until expiry.
            # A genuinely superseded lease is rejected by the server's token check.
            with contextlib.suppress(
                httpx.HTTPError, TimeoutError, LeaseLostError, TaskCancelledError
            ):
                async with asyncio.timeout(15):
                    await _submit_failure(client, task, "ControlPlaneHttpError", retryable=True)
            logger.info("task lease was lost", extra={"task_id": task_id})
            return
        except Exception:
            # Malformed heartbeat replies must not leave execution detached.
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
    finished.set()
    heartbeat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat
    try:
        await execution
    except LeaseLostError:
        logger.info("task lease was superseded", extra={"task_id": task_id})
    except TaskCancelledError:
        with contextlib.suppress(httpx.HTTPError):
            await _submit_failure(client, task, "TaskCancelled", retryable=False)
    except httpx.HTTPStatusError as exc:
        response_detail: str | None = None
        try:
            response_payload = exc.response.json()
            if isinstance(response_payload, dict):
                raw_detail = response_payload.get("detail")
                if isinstance(raw_detail, str):
                    response_detail = raw_detail[:240]
                elif isinstance(raw_detail, list):
                    parts: list[str] = []
                    for item in raw_detail[:4]:
                        if isinstance(item, dict):
                            loc = ".".join(str(p) for p in item.get("loc", []))
                            msg = str(item.get("msg", ""))
                            parts.append(f"{loc}: {msg}" if loc else msg)
                        elif isinstance(item, str):
                            parts.append(item)
                    response_detail = "; ".join(parts)[:240] if parts else None
        except ValueError, TypeError:
            pass
        permanent_rejection = 400 <= exc.response.status_code < 500
        error_class = (
            "ControlPlanePayloadRejected" if permanent_rejection else "ControlPlaneHttpError"
        )
        logger.warning(
            "control plane rejected worker task progress",
            extra={
                "task_id": task_id,
                "status_code": exc.response.status_code,
                "method": exc.request.method,
                "path": exc.request.url.path,
                "error_class": error_class,
                "reason": response_detail,
            },
        )
        with contextlib.suppress(httpx.HTTPError, LeaseLostError, TaskCancelledError):
            await _renew_lease(client, task, stage="failed")
            await _submit_failure(
                client,
                task,
                error_class,
                retryable=not permanent_rejection,
                reason=response_detail,
            )
    except Exception as exc:
        error_class = type(exc).__name__
        retryable = _task_failure_is_retryable(exc)
        if isinstance(exc, LocalModelOutOfMemoryError) and provider is not None:
            target = task.get("target_ref")
            model_key = target.get("model_key") if isinstance(target, dict) else None
            if isinstance(model_key, str):
                # Release local allocations before returning replaceable capacity
                # work. The next lease reloads the model under the fitted profile.
                with contextlib.suppress(LocalInferenceError, ValueError):
                    await provider.unload(model_key)
        if isinstance(exc, LocalInferenceError):
            logger.warning(
                "local inference task failed",
                extra={
                    "task_id": task_id,
                    "error_class": error_class,
                    "reason": str(exc),
                },
            )
        elif isinstance(exc, SourceMediaUnavailableError):
            # The coordinator already told us exactly why the picture was
            # unusable, and throwing that away left the owner reading a generic
            # sentence and searching Immich for a fault that may not be there.
            logger.warning(
                "source media could not be used",
                extra={
                    "task_id": task_id,
                    "error_class": error_class,
                    "source_error_code": exc.error_code,
                    "asset_id": exc.asset_id,
                },
            )
        else:
            logger.exception(
                "worker task failed unexpectedly",
                extra={"task_id": task_id, "error_class": error_class},
            )
        with contextlib.suppress(httpx.HTTPError, LeaseLostError, TaskCancelledError):
            await _renew_lease(client, task, stage="failed")
            await _submit_failure(
                client,
                task,
                error_class,
                retryable=retryable,
                reason=str(exc) if isinstance(exc, LocalInferenceError) else None,
                source_asset_id=(
                    exc.asset_id if isinstance(exc, SourceMediaUnavailableError) else None
                ),
                source_error_code=(
                    exc.error_code if isinstance(exc, SourceMediaUnavailableError) else None
                ),
            )


async def _process_task(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    provider: LocalInferenceProvider | None,
) -> None:
    task_type = str(task.get("task_type") or "unknown")
    try:
        async with asyncio.timeout(task_wall_seconds(task_type)):
            await _process_task_inner(client, task, provider)
    except TimeoutError:
        logger.warning("worker task wall deadline reached", extra={"task_type": task_type})
        with contextlib.suppress(httpx.HTTPError, TimeoutError, LeaseLostError, TaskCancelledError):
            async with asyncio.timeout(15):
                await _submit_failure(client, task, "TaskWallTimeExceeded", retryable=True)
