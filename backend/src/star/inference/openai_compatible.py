from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Collection, Mapping
from typing import Literal
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from star.inference.policy import (
    SYSTEM_POLICY,
    LocalInferenceEnvelopeError,
    LocalInferenceJsonError,
    LocalInferenceSchemaError,
    LocalInferenceTransportError,
    enforce_cross_field_consistency,
    normalize_schema_string_lengths,
    runtime_generation_schema,
    schema_validation_issue,
    validate_structured_request,
)
from star.inference.provider import (
    LocalModelInventoryItem,
    StructuredInferenceRequest,
    StructuredInferenceResult,
    sampling_profile_for,
    stable_inference_seed,
)


def _normalize_artifact_checksum(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 71 and value.lower().startswith("sha256:"):
        digest = value[7:]
        if all(character in "0123456789abcdef" for character in digest.lower()):
            return "sha256:" + digest.lower()
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    ):
        return "sha256:" + value.lower()
    return None


class OpenAICompatibleLocalProvider:
    """Local-only adapter for MLX/LM Studio/llama.cpp OpenAI-compatible servers."""

    def __init__(
        self,
        *,
        allowed_models: Collection[str],
        base_url: str = "http://127.0.0.1:8080",
        allowed_hosts: Collection[str] = ("127.0.0.1", "::1", "localhost"),
        api_key: str | None = None,
        model_aliases: dict[str, str] | None = None,
        artifact_checksums: Mapping[str, Mapping[str, str]] | None = None,
        idle_ttl_seconds: int = 3600,
        load_context_length: int | None = None,
        load_parallel: int | None = None,
        # The bounded effort substituted for the historical internal ``high``
        # flag. Star never asks a runtime for high or xhigh reasoning, so the
        # only accepted values are the two bounded levels.
        high_reasoning_effort: Literal["low", "medium"] = "medium",
        timeout_seconds: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
        # A fixed runtime label for provenance ("mlx-vlm", "llama.cpp-prism");
        # without one the inventory read decides between LM Studio and mlx.
        runtime_name: str | None = None,
        # A llama.cpp server started with --sleep-idle-seconds frees its model
        # after that many idle seconds and reloads it on the next request. When
        # set, unload() means "wait until it has gone to sleep" so the GPU is
        # free before another runtime loads its model.
        sleep_idle_seconds: int | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in set(allowed_hosts)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("model endpoint must be an explicitly allowed local host")
        self._allowed_models = frozenset(allowed_models)
        if not self._allowed_models:
            raise ValueError("at least one allow-listed local model is required")
        self._model_aliases = dict(model_aliases or {})
        if not set(self._model_aliases).issubset(self._allowed_models):
            raise ValueError("model alias keys must be allow-listed canonical models")
        if any(not value.strip() for value in self._model_aliases.values()):
            raise ValueError("runtime model aliases cannot be empty")
        # Set by the first inventory read: LM Studio answers /api/v1/models, a
        # native mlx-vlm/mlx-lm server only /v1/models. Results must carry the
        # same runtime name the inventory registered or the coordinator rejects them.
        self._runtime_name: str | None = None
        if runtime_name is not None and not runtime_name.strip():
            raise ValueError("runtime name cannot be empty")
        self._configured_runtime_name = runtime_name.strip() if runtime_name else None
        if sleep_idle_seconds is not None and not 1 <= sleep_idle_seconds <= 3600:
            raise ValueError("sleep idle seconds must be between 1 and 3600")
        self._sleep_idle_seconds = sleep_idle_seconds
        self._artifact_checksums: dict[str, dict[str, str]] = {}
        for model_key, versions in (artifact_checksums or {}).items():
            if model_key not in self._allowed_models or not isinstance(versions, Mapping):
                raise ValueError("artifact checksum mapping contains an unapproved model")
            normalized_versions: dict[str, str] = {}
            for version, checksum in versions.items():
                if not isinstance(version, str) or not version.strip():
                    raise ValueError("artifact checksum mapping contains an invalid version")
                normalized = _normalize_artifact_checksum(checksum)
                if normalized is None:
                    raise ValueError("artifact checksum mapping contains an invalid checksum")
                normalized_versions[version] = normalized
            self._artifact_checksums[model_key] = normalized_versions
        if idle_ttl_seconds < 0:
            raise ValueError("model idle TTL cannot be negative")
        self._idle_ttl_seconds = idle_ttl_seconds
        if load_context_length is not None and load_context_length < 1024:
            raise ValueError("managed model context length must be at least 1024")
        if load_parallel is not None and not 1 <= load_parallel <= 8:
            raise ValueError("managed model parallelism must be between 1 and 8")
        if (load_context_length is None) != (load_parallel is None):
            raise ValueError("managed model context and parallelism must be configured together")
        self._load_context_length = load_context_length
        self._load_parallel = load_parallel
        if high_reasoning_effort not in {"low", "medium"}:
            raise ValueError("reasoning effort above medium is never requested from a runtime")
        self._high_reasoning_effort: Literal["low", "medium"] = high_reasoning_effort
        self._managed_load_lock = asyncio.Lock()
        self._verified_loaded_models: set[str] = set()
        normalized_api_key = api_key.strip() if api_key else ""
        if normalized_api_key and (
            len(normalized_api_key) > 4096
            or "\n" in normalized_api_key
            or "\r" in normalized_api_key
        ):
            raise ValueError("local model API key is invalid")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=(
                {"Authorization": f"Bearer {normalized_api_key}"} if normalized_api_key else None
            ),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> OpenAICompatibleLocalProvider:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _configured_checksum(self, model_key: str, version: str) -> str | None:
        """An owner-configured checksum for this model; ``*`` covers every version."""

        versions = self._artifact_checksums.get(model_key, {})
        return versions.get(version) or versions.get("*")

    def _runtime_label(self, detected: str) -> str:
        # A configured label folds onto the runtime it stands for so results
        # match the registration the coordinator already holds.
        from star.analysis.asset_vlm_submission import canonical_runtime

        return canonical_runtime(self._configured_runtime_name) or detected

    def _runtime_model(self, canonical_model: str) -> str:
        return self._model_aliases.get(canonical_model, canonical_model)

    def _canonical_model(self, runtime_model: str) -> str:
        for canonical, runtime in self._model_aliases.items():
            if runtime == runtime_model:
                return canonical
        return runtime_model

    async def health(self) -> bool:
        inventory = await self.inventory()
        approved = [item for item in inventory if item.model_key in self._allowed_models]
        if approved:
            # Inventory proves the authenticated native runtime is reachable.
            # Do not eagerly load every approved model: LM Studio may be configured
            # to keep only the last JIT model, and health polling must never cause
            # expensive model residency thrashing.
            return True
        try:
            response = await self._client.get("/v1/models")
            return response.status_code == 200 and isinstance(response.json().get("data"), list)
        except httpx.HTTPError, json.JSONDecodeError, AttributeError:
            return False

    async def _ensure_managed_load(self, model_key: str) -> None:
        """Load LM Studio models with bounded context and one prediction slot."""

        if self._load_context_length is None or self._load_parallel is None:
            return
        runtime_model = self._runtime_model(model_key)
        async with self._managed_load_lock:
            try:
                response = await self._client.get("/api/v1/models")
                response.raise_for_status()
                models = response.json().get("models")
                if not isinstance(models, list):
                    raise TypeError
                self._runtime_name = "openai-compatible"
                model = next(
                    (
                        item
                        for item in models
                        if isinstance(item, dict) and item.get("key") == runtime_model
                    ),
                    None,
                )
                if model is None:
                    raise LocalInferenceTransportError("configured local model is unavailable")
                loaded_by_model: list[tuple[str, str]] = []
                for inventory_model in models:
                    if not isinstance(inventory_model, dict):
                        continue
                    inventory_key = inventory_model.get("key")
                    instances = inventory_model.get("loaded_instances")
                    if not isinstance(inventory_key, str) or not isinstance(instances, list):
                        continue
                    for instance in instances:
                        instance_id = instance.get("id") if isinstance(instance, dict) else None
                        if isinstance(instance_id, str) and instance_id:
                            loaded_by_model.append((inventory_key, instance_id))
                target_instances = [
                    instance_id
                    for loaded_key, instance_id in loaded_by_model
                    if loaded_key == runtime_model
                ]
                if (
                    len(loaded_by_model) == 1
                    and target_instances
                    and model_key in self._verified_loaded_models
                ):
                    return
                for _loaded_key, instance_id in loaded_by_model:
                    unload = await self._client.post(
                        "/api/v1/models/unload",
                        json={"instance_id": instance_id},
                    )
                    unload.raise_for_status()
                self._verified_loaded_models.clear()
                load = await self._client.post(
                    "/api/v1/models/load",
                    json={
                        "model": runtime_model,
                        "context_length": self._load_context_length,
                        "parallel": self._load_parallel,
                        "echo_load_config": True,
                    },
                )
                load.raise_for_status()
            except (httpx.HTTPError, json.JSONDecodeError, AttributeError, TypeError) as exc:
                raise LocalInferenceTransportError(
                    "local model could not be loaded with the required runtime bounds"
                ) from exc
            self._verified_loaded_models.add(model_key)

    async def _wait_for_sleep(self) -> None:
        """Wait until a --sleep-idle-seconds server has released its model.

        The server sleeps on its own after the idle interval; this only waits
        for it, polling /health, and gives up after the interval plus a margin
        so a runtime that never reports sleeping cannot block the lane.
        """

        assert self._sleep_idle_seconds is not None
        deadline = time.monotonic() + self._sleep_idle_seconds + 15
        while time.monotonic() < deadline:
            try:
                response = await self._client.get("/health")
                body = response.json() if response.content else {}
                status_text = str(body.get("status", "")).lower() if isinstance(body, dict) else ""
                if response.status_code == 503 or "sleep" in status_text:
                    return
            except httpx.HTTPError, json.JSONDecodeError, ValueError:
                return
            await asyncio.sleep(1.0)

    async def warmup(self, model_key: str, *, system_prompt: str | None = None) -> None:
        """Trigger local JIT model loading with a media-free bounded request."""

        if model_key not in self._allowed_models:
            raise ValueError("model is not allow-listed for this provider")
        await self._ensure_managed_load(model_key)
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                json={
                    "model": self._runtime_model(model_key),
                    "messages": [
                        *([{"role": "system", "content": system_prompt}] if system_prompt else []),
                        {"role": "user", "content": "Reply OK."},
                    ],
                    "temperature": 0,
                    "max_tokens": 1,
                    "stream": False,
                    "ttl": self._idle_ttl_seconds,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalInferenceTransportError("local model warm-up failed") from exc

    async def unload(self, model_key: str) -> None:
        """Unload every managed instance for one approved local LM Studio model."""

        if model_key not in self._allowed_models:
            raise ValueError("model is not allow-listed for this provider")
        if self._sleep_idle_seconds is not None:
            await self._wait_for_sleep()
            return
        runtime_model = self._runtime_model(model_key)
        async with self._managed_load_lock:
            try:
                response = await self._client.get("/api/v1/models")
                response.raise_for_status()
                models = response.json().get("models")
                if not isinstance(models, list):
                    raise TypeError
                self._runtime_name = "openai-compatible"
                model = next(
                    (
                        item
                        for item in models
                        if isinstance(item, dict) and item.get("key") == runtime_model
                    ),
                    None,
                )
                instances = model.get("loaded_instances", []) if isinstance(model, dict) else []
                instances = instances if isinstance(instances, list) else []
                for instance in instances:
                    instance_id = instance.get("id") if isinstance(instance, dict) else None
                    if isinstance(instance_id, str) and instance_id:
                        unloaded = await self._client.post(
                            "/api/v1/models/unload",
                            json={"instance_id": instance_id},
                        )
                        unloaded.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Plain MLX OpenAI servers expose no LM Studio management API.
                # Their host supervisor owns process residency, so unloading is
                # intentionally a safe no-op rather than a failed photo task.
                if exc.response.status_code in {404, 405}:
                    try:
                        unloaded = await self._client.post("/unload")
                        if unloaded.status_code not in {200, 404, 405}:
                            unloaded.raise_for_status()
                    except httpx.HTTPError as unload_exc:
                        raise LocalInferenceTransportError(
                            "local model could not be unloaded"
                        ) from unload_exc
                    self._verified_loaded_models.discard(model_key)
                    return
                raise LocalInferenceTransportError("local model could not be unloaded") from exc
            except (httpx.HTTPError, json.JSONDecodeError, AttributeError, TypeError) as exc:
                raise LocalInferenceTransportError("local model could not be unloaded") from exc
            self._verified_loaded_models.discard(model_key)

    async def inventory(self) -> list[LocalModelInventoryItem]:
        """Read LM Studio's bounded native inventory without discovering unapproved models."""

        try:
            response = await self._client.get("/api/v1/models")
            response.raise_for_status()
            if len(response.content) > 2 * 1024 * 1024:
                return []
            response_value = response.json()
            models = response_value.get("models")
            runtime_version_value = response_value.get("runtime_version") or response_value.get(
                "version"
            )
            runtime_version = (
                runtime_version_value if isinstance(runtime_version_value, str) else None
            )
        except httpx.HTTPError, json.JSONDecodeError, AttributeError:
            return await self._openai_inventory()
        if not isinstance(models, list):
            return await self._openai_inventory()
        self._runtime_name = "openai-compatible"
        inventory: list[LocalModelInventoryItem] = []
        for value in models[:256]:
            if not isinstance(value, dict) or value.get("type") != "llm":
                continue
            runtime_model_key = value.get("key")
            if not isinstance(runtime_model_key, str):
                continue
            model_key = self._canonical_model(runtime_model_key)
            if model_key not in self._allowed_models:
                continue
            quantization_value = value.get("quantization")
            quantization = (
                quantization_value.get("name") if isinstance(quantization_value, dict) else None
            )
            capabilities = value.get("capabilities")
            capabilities = capabilities if isinstance(capabilities, dict) else {}
            reasoning_value = capabilities.get("reasoning")
            selected_variant = value.get("selected_variant")
            runtime_format = value.get("format")
            architecture = value.get("architecture")
            size_bytes = value.get("size_bytes")
            max_context_length = value.get("max_context_length")
            loaded_instances = value.get("loaded_instances")
            version = selected_variant if isinstance(selected_variant, str) else model_key
            artifact_checksum = _normalize_artifact_checksum(
                value.get("sha256") or value.get("digest")
            )
            configured_checksum = self._artifact_checksums.get(model_key, {}).get(version)
            if (
                artifact_checksum
                and configured_checksum
                and artifact_checksum != configured_checksum
            ):
                artifact_checksum = None
            elif configured_checksum:
                artifact_checksum = configured_checksum
            inventory.append(
                LocalModelInventoryItem(
                    model_key=model_key,
                    version=version,
                    runtime=self._runtime_label("openai-compatible"),
                    quantization=(
                        quantization if isinstance(quantization, str) else "runtime-managed"
                    ),
                    runtime_format=(
                        runtime_format if isinstance(runtime_format, str) else "unknown"
                    ),
                    architecture=architecture if isinstance(architecture, str) else None,
                    size_bytes=(
                        size_bytes if isinstance(size_bytes, int) and size_bytes >= 0 else None
                    ),
                    max_context_length=(
                        max_context_length
                        if isinstance(max_context_length, int) and max_context_length > 0
                        else None
                    ),
                    vision=capabilities.get("vision") is True,
                    reasoning=isinstance(reasoning_value, dict),
                    loaded=isinstance(loaded_instances, list) and bool(loaded_instances),
                    runtime_version=runtime_version,
                    artifact_checksum=artifact_checksum,
                    seed_supported=True,
                )
            )
        return inventory

    async def _openai_inventory(self) -> list[LocalModelInventoryItem]:
        """Read the standard endpoint used by native mlx-vlm/mlx-lm servers."""

        self._runtime_name = "mlx"

        def mlx_quantization(runtime_model: str, value: dict[str, object] | None = None) -> str:
            raw = value.get("quantization") if value is not None else None
            if isinstance(raw, str) and raw:
                return raw
            normalized = runtime_model.lower()
            if "8bit" in normalized or "8-bit" in normalized:
                return "8bit"
            if "4bit" in normalized or "4-bit" in normalized:
                return "4bit"
            # The stable active alias intentionally hides which validated
            # snapshot it points at; report that honestly instead of claiming
            # the former 4-bit-only contract.
            return "runtime-managed"

        try:
            response = await self._client.get("/v1/models")
            response.raise_for_status()
            if len(response.content) > 2 * 1024 * 1024:
                return []
            values = response.json().get("data")
        except httpx.HTTPError, json.JSONDecodeError, AttributeError:
            return []
        if not isinstance(values, list):
            return []
        inventory: list[LocalModelInventoryItem] = []
        for value in values[:256]:
            if not isinstance(value, dict):
                continue
            runtime_model = value.get("id")
            if not isinstance(runtime_model, str):
                continue
            model_key = self._canonical_model(runtime_model)
            if model_key not in self._allowed_models:
                continue
            inventory.append(
                LocalModelInventoryItem(
                    model_key=model_key,
                    version=str(value.get("version") or runtime_model),
                    runtime=self._runtime_label("mlx"),
                    quantization=mlx_quantization(runtime_model, value),
                    runtime_format="mlx",
                    architecture="qwen3.8",
                    size_bytes=None,
                    max_context_length=32768,
                    vision=True,
                    reasoning=True,
                    loaded=bool(value.get("loaded", True)),
                    runtime_version=(
                        str(value["runtime_version"]) if value.get("runtime_version") else None
                    ),
                    artifact_checksum=self._configured_checksum(
                        model_key, str(value.get("version") or runtime_model)
                    ),
                    seed_supported=True,
                )
            )
        # mlx-vlm lazily loads an explicitly configured filesystem model and
        # therefore may not list it before its first request. Absolute aliases
        # are owner-controlled local paths; advertise only those pinned paths.
        present = {item.model_key for item in inventory}
        for model_key in sorted(self._allowed_models - present):
            runtime_model = self._runtime_model(model_key)
            if not runtime_model.startswith("/"):
                continue
            inventory.append(
                LocalModelInventoryItem(
                    model_key=model_key,
                    version=runtime_model,
                    runtime=self._runtime_label("mlx"),
                    quantization=mlx_quantization(runtime_model),
                    runtime_format="mlx",
                    architecture="qwen3.8",
                    size_bytes=None,
                    max_context_length=32768,
                    vision=True,
                    reasoning=True,
                    loaded=False,
                    artifact_checksum=self._configured_checksum(model_key, runtime_model),
                    seed_supported=True,
                )
            )
        return inventory

    def _bounded_reasoning_effort(
        self, request: StructuredInferenceRequest
    ) -> Literal["low", "medium"] | None:
        """Map the request effort to the bounded runtime value, never above medium."""

        if request.reasoning_effort == "off":
            return None
        if request.reasoning_effort in {"low", "medium"}:
            return request.reasoning_effort
        return self._high_reasoning_effort

    async def infer(self, request: StructuredInferenceRequest) -> StructuredInferenceResult:
        if request.model_key not in self._allowed_models:
            raise ValueError("model is not allow-listed for this provider")
        # ``context_tokens_override`` has no per-request equivalent on this
        # path: LM Studio binds the context length when the model is loaded
        # (``_ensure_managed_load`` with the configured ``load_context_length``),
        # and the native ``mlx_vlm.server`` fixes its KV budget with the
        # ``--max-kv-size`` launch flag. The override is therefore recorded in
        # the effective settings but intentionally not sent, never failed.
        await self._ensure_managed_load(request.model_key)
        validated = validate_structured_request(request)
        system_prompt = request.system_prompt_override or SYSTEM_POLICY
        content: list[dict[str, object]] = [{"type": "text", "text": validated.trusted_prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:"
                    + item.media_type
                    + ";base64,"
                    + base64.b64encode(item.content).decode("ascii"),
                },
            }
            for item in request.media
        )
        content.append({"type": "text", "text": validated.evidence_prompt})
        reasoning_effort = self._bounded_reasoning_effort(request)
        is_thinking = reasoning_effort is not None
        sampling = sampling_profile_for(request, is_thinking=is_thinking)
        body: dict[str, object] = {
            "model": self._runtime_model(request.model_key),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "min_p": sampling.min_p,
            "repeat_penalty": sampling.repeat_penalty,
            # mlx_vlm.server reads ``repetition_penalty``; LM Studio reads
            # ``repeat_penalty``. Send both so the same penalty applies on
            # either runtime.
            "repetition_penalty": sampling.repeat_penalty,
            "seed": stable_inference_seed(request),
            "max_tokens": request.max_completion_tokens,
            "stream": False,
            "ttl": self._idle_ttl_seconds,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "star_result",
                    "strict": True,
                    "schema": runtime_generation_schema(request.schema),
                },
            },
        }
        if sampling.presence_penalty:
            # Sent only when non-zero so production bodies stay byte-identical.
            # Both LM Studio and mlx_vlm.server read ``presence_penalty``.
            body["presence_penalty"] = sampling.presence_penalty
        # Thinking control, verified against the installed Apple runtime
        # (mlx_vlm 0.6.17, ``mlx_vlm/server/request_normalization.py``) and the
        # LM Studio OpenAI-compatible API:
        #   * mlx_vlm.server reads a top-level ``enable_thinking`` boolean with
        #     the highest precedence, then ``reasoning_effort`` (any value not in
        #     {none, off, disabled, false, 0} enables thinking and is passed to
        #     the chat template), and ignores unknown keys (``extra="allow"``).
        #     It has no ``chat_template_kwargs`` field.
        #   * LM Studio (and mlx-lm / vLLM style servers) read
        #     ``chat_template_kwargs: {"enable_thinking": ...}`` and accept
        #     ``reasoning_effort`` only for the bounded named levels.
        # ``off`` therefore sends both disable forms and no ``reasoning_effort``
        # (LM Studio may reject the non-standard value "none"); low/medium send
        # the bounded ``reasoning_effort`` plus both enable forms. High and
        # xhigh are never sent.
        if reasoning_effort is None:
            body["enable_thinking"] = False
            body["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            body["reasoning_effort"] = reasoning_effort
            body["enable_thinking"] = True
            body["chat_template_kwargs"] = {"enable_thinking": True}
        started = time.monotonic()
        try:
            response = await self._client.post("/v1/chat/completions", json=body)
            if response.status_code == 400:
                # Some LM Studio MLX vision backends reject JSON grammar
                # compilation before inference. The trusted prompt already
                # contains the complete schema, so retry once without the
                # runtime grammar and retain the same strict local validation.
                fallback_body = dict(body)
                fallback_body.pop("response_format", None)
                response = await self._client.post(
                    "/v1/chat/completions",
                    json=fallback_body,
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalInferenceTransportError("local model HTTP request failed") from exc
        try:
            response_value = response.json()
            message = response_value["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LocalInferenceEnvelopeError(
                "local model response did not contain an assistant message"
            ) from exc

        model_output = message.get("content")
        # LM Studio reasoning models can put schema-constrained JSON in
        # reasoning_content while returning an empty content string. This is still
        # untrusted model output and goes through the exact same size, JSON, and
        # JSON-Schema validation as normal assistant content.
        if model_output is None or model_output == "":
            model_output = message.get("reasoning_content")
        if model_output is None or model_output == "":
            raise LocalInferenceEnvelopeError(
                "local model response did not contain structured output"
            )
        if isinstance(model_output, str):
            if len(model_output.encode("utf-8")) > 1024 * 1024:
                raise LocalInferenceEnvelopeError("local model response exceeded the safety limit")
            try:
                value = json.loads(model_output)
            except json.JSONDecodeError as exc:
                raise LocalInferenceJsonError(
                    "local model structured output was not valid JSON"
                ) from exc
        else:
            value = model_output
        if not isinstance(value, dict):
            raise LocalInferenceEnvelopeError("local model returned a non-object result")
        value, _normalized_paths = normalize_schema_string_lengths(value, request.schema)
        value, _consistency_corrections = enforce_cross_field_consistency(value)
        try:
            Draft202012Validator(request.schema).validate(value)
        except ValidationError as exc:
            raise LocalInferenceSchemaError(
                "local model structured output did not match the required schema "
                f"({schema_validation_issue(exc)})"
            ) from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = response_value.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if self._runtime_name is None:
            await self.inventory()
        return StructuredInferenceResult(
            value=value,
            model_version=request.model_key,
            runtime=self._runtime_label(self._runtime_name or "openai-compatible"),
            duration_ms=max(duration_ms, 0),
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            request_count=1,
            effective_settings={
                "runtime": self._runtime_label(self._runtime_name or "openai-compatible"),
                "sampling_profile": sampling.name,
                "temperature": sampling.temperature,
                "top_p": sampling.top_p,
                "top_k": sampling.top_k,
                "min_p": sampling.min_p,
                "repeat_penalty": sampling.repeat_penalty,
                "presence_penalty": sampling.presence_penalty,
                "seed": body["seed"],
                "max_tokens": request.max_completion_tokens,
                "context_tokens_override": request.context_tokens_override,
                "context_tokens_applied": False,
                "load_context_length": self._load_context_length,
                "reasoning_effort": reasoning_effort or "off",
                "thinking": is_thinking,
                "thinking_control": "enable_thinking+chat_template_kwargs",
            },
        )
