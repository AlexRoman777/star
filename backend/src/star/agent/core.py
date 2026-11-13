from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from star.agent._base import MODEL_CAPABILITIES, logger
from star.agent.capacity import _capabilities
from star.agent.identity import PowerAdmissionState, _identity, _ssl_context
from star.agent.inference_tasks import validate_agent_inference_contracts
from star.agent.queue import (
    ModelResidencyManager,
    QuietHoursPolicy,
    _AdaptiveAppleQueue,
    _establish_worker_session,
    _lane_routes,
    _run_lane,
    _run_model_inventory_sync,
    _run_worker_heartbeat,
    _wait_for_worker_supervisor,
    _WorkerRuntimeControls,
    parallel_lane_model_keys,
)
from star.inference.host_power import HostPowerError, HttpHostPowerProvider
from star.inference.ollama import OllamaLocalProvider
from star.inference.openai_compatible import OpenAICompatibleLocalProvider
from star.inference.policy import LocalInferenceError
from star.inference.provider import (
    LocalInferenceProvider,
)
from star.inference.routed import RoutedLocalInferenceProvider


async def _refresh_host_power_status(
    provider: HttpHostPowerProvider,
    state: PowerAdmissionState,
    changed: asyncio.Event,
    host_current_load: dict[str, float] | None = None,
) -> None:
    try:
        status = await provider.power_status()
    except HostPowerError:
        state.update(
            power_source="unknown",
            battery_percent=None,
            charging=None,
            charger_wattage=None,
        )
    else:
        state.update(
            power_source=status.power_source,
            battery_percent=status.battery_percent,
            charging=status.charging,
            charger_wattage=status.charger_wattage,
        )
        if host_current_load is not None:
            host_current_load.clear()
            host_current_load.update(status.host_load)
    changed.set()


async def _run_host_power_sync(
    provider: HttpHostPowerProvider,
    state: PowerAdmissionState,
    changed: asyncio.Event,
    stop: asyncio.Event,
    host_current_load: dict[str, float] | None = None,
) -> None:
    while not stop.is_set():
        await _refresh_host_power_status(provider, state, changed, host_current_load)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=15.0)


def _adviser_artifact_checksums(model_key: str, raw: str) -> dict[str, dict[str, str]] | None:
    """The owner-verified checksum of the installed MLX archive, for every served version.

    The MLX server cannot report one itself, and the archive lives outside the
    worker container, so the install script records it and the worker reports it
    with the model inventory. Without it the coordinator can never enable the
    MLX registration automatically.
    """

    value = raw.strip()
    if not value:
        return None
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise RuntimeError("adviser model artifact checksum must be a hex SHA-256 digest")
    return {model_key: {"*": f"sha256:{value.lower()}"}}


def _parse_model_artifact_checksums(raw: str) -> dict[str, dict[str, str]]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("model artifact checksums must be valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("model artifact checksums must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for model_key, versions in value.items():
        if not isinstance(model_key, str) or not isinstance(versions, dict):
            raise RuntimeError("model artifact checksums must map models to version objects")
        if not all(
            isinstance(version, str) and isinstance(checksum, str)
            for version, checksum in versions.items()
        ):
            raise RuntimeError("model artifact checksum entries must be strings")
        result[model_key] = dict(versions)
    return result


def _parallel_lane_allowance(raw: str, model_keys: Collection[str]) -> dict[str, int]:
    """Read ``model=lanes`` pairs into the measured per-model lane allowance."""

    allowance: dict[str, int] = {}
    for item in str(raw or "").split(","):
        entry = item.strip()
        if not entry:
            continue
        key, _, lanes = entry.rpartition("=")
        if not key or not lanes.isdigit():
            raise RuntimeError("parallel model lanes must be model=lanes pairs")
        if key not in model_keys:
            raise RuntimeError(f"parallel model lane key is not configured: {key}")
        allowance[key] = max(1, int(lanes))
    return allowance


async def run_agent(args: argparse.Namespace) -> None:
    validate_agent_inference_contracts()
    if not 0 <= args.heavy_task_cooldown_seconds <= 3600:
        raise RuntimeError("heavy task cooldown must be between 0 and 3600 seconds")
    quiet_hours: QuietHoursPolicy | None = None
    if args.quiet_hours_cooldown_seconds > 0 or args.quiet_hours_performance_percent < 100:
        try:
            quiet_hours = QuietHoursPolicy.parse(
                timezone=args.quiet_hours_timezone,
                start=args.quiet_hours_start,
                end=args.quiet_hours_end,
                cooldown_seconds=args.quiet_hours_cooldown_seconds,
                performance_percent=args.quiet_hours_performance_percent,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
    token_path = Path(args.worker_token_file)
    identity_path = Path(args.identity_file or str(token_path) + ".identity")
    runtime_session_id = uuid.uuid4()
    runtime_session_started_at = datetime.now(UTC)
    capabilities = _capabilities(args.capabilities)
    accepted = sorted(capabilities)
    vlm_capabilities = MODEL_CAPABILITIES
    configured_model_keys = args.model_key or []
    if isinstance(configured_model_keys, str):
        configured_model_keys = [configured_model_keys]
    configured_model_keys.extend(
        item.strip() for item in args.model_key_csv.split(",") if item.strip()
    )
    model_keys = list(dict.fromkeys(configured_model_keys))
    artifact_checksums = _parse_model_artifact_checksums(args.model_artifact_checksums)
    model_aliases: dict[str, str] = {}
    raw_model_aliases = list(args.model_runtime_alias)
    raw_model_aliases.extend(
        item.strip()
        for item in getattr(args, "model_runtime_alias_csv", "").split(",")
        if item.strip()
    )
    for raw_alias in raw_model_aliases:
        canonical, separator, runtime_model = raw_alias.partition("=")
        if not separator or not canonical.strip() or not runtime_model.strip():
            raise RuntimeError("model runtime aliases must use CANONICAL=RUNTIME")
        model_aliases[canonical.strip()] = runtime_model.strip()
    if not set(model_aliases).issubset(model_keys):
        raise RuntimeError("model runtime alias keys must also be configured model keys")
    if args.ollama_model and args.ollama_model not in model_keys:
        model_keys.append(args.ollama_model)
    if set(capabilities).intersection(vlm_capabilities) and not model_keys:
        raise RuntimeError("a VLM capability requires --model-key")
    provider: LocalInferenceProvider | None = None
    adviser_model_key = getattr(args, "adviser_model_key", None)
    adviser_model_url = getattr(args, "adviser_model_url", None)
    if model_keys:
        allowed_hosts = {
            "127.0.0.1",
            "::1",
            "localhost",
            *args.model_allowed_host,
            *args.ollama_allowed_host,
        }
        if args.model_provider == "openai-compatible":
            provider = OpenAICompatibleLocalProvider(
                allowed_models=set(model_keys),
                base_url=args.model_url or "http://127.0.0.1:8080",
                allowed_hosts=allowed_hosts,
                api_key=os.getenv("STAR_MODEL_API_KEY"),
                model_aliases=model_aliases,
                artifact_checksums=artifact_checksums,
                idle_ttl_seconds=args.model_idle_ttl_seconds,
                load_context_length=args.model_load_context_length,
                load_parallel=args.model_load_parallel,
            )
        else:
            provider = OllamaLocalProvider(
                allowed_models=set(model_keys),
                base_url=args.model_url or args.ollama_url,
                allowed_hosts=allowed_hosts,
                keep_alive=args.model_keep_alive,
                require_full_accelerator_residency=(args.require_full_accelerator_residency),
            )
        if adviser_model_key or adviser_model_url:
            if not adviser_model_key or not adviser_model_url:
                raise RuntimeError("adviser model key and URL must be configured together")
            if adviser_model_key not in model_keys:
                raise RuntimeError("adviser model key must also be a configured model key")
            adviser_alias = getattr(args, "adviser_model_runtime_alias", None)
            adviser_provider = OpenAICompatibleLocalProvider(
                allowed_models={adviser_model_key},
                base_url=adviser_model_url,
                allowed_hosts={
                    "127.0.0.1",
                    "::1",
                    "localhost",
                    *getattr(args, "adviser_model_allowed_host", []),
                },
                api_key=os.getenv("STAR_ADVISER_MODEL_API_KEY"),
                model_aliases=({adviser_model_key: adviser_alias} if adviser_alias else None),
                artifact_checksums=_adviser_artifact_checksums(
                    adviser_model_key, getattr(args, "adviser_model_artifact_checksum", "")
                ),
                idle_ttl_seconds=args.model_idle_ttl_seconds,
                high_reasoning_effort="medium",
                runtime_name=getattr(args, "adviser_model_runtime_name", None) or "mlx",
                sleep_idle_seconds=getattr(args, "adviser_model_sleep_idle_seconds", None),
            )
            provider = RoutedLocalInferenceProvider(
                default=provider,
                model_routes={adviser_model_key: adviser_provider},
            )
    # Models are loaded only by leased warm-up or inference tasks.  This keeps the
    # queue lane aware of the resident model so it can explicitly unload it before
    # switching, and avoids loading the first configured model when another stage
    # is actually at the head of the queue.
    power_status_provider: HttpHostPowerProvider | None = None
    power_status = PowerAdmissionState()
    if args.battery_resume_percent and args.battery_resume_percent < args.minimum_battery:
        raise RuntimeError("battery resume percentage cannot be below the battery floor")
    if args.power_status_url:
        # The Apple worker power service token; the Apple Vision name is accepted so
        # an Apple worker .env from before v209 round 3 keeps working until the
        # deploy tool rewrites it.
        power_token = os.getenv("STAR_APPLE_POWER_TOKEN", "") or os.getenv(
            "STAR_APPLE_VISION_TOKEN", ""
        )
        power_status_provider = HttpHostPowerProvider(
            base_url=args.power_status_url,
            allowed_hosts={
                "127.0.0.1",
                "::1",
                "localhost",
                *args.power_status_allowed_host,
            },
            api_key=power_token,
        )
    technical_concurrency = args.technical_concurrency
    if technical_concurrency is None:
        technical_concurrency = max(1, min(4, (os.cpu_count() or 2) // 2))
    lanes = _lane_routes(
        set(capabilities),
        technical_concurrency=technical_concurrency,
        model_concurrency=args.model_concurrency,
    )
    if not lanes:
        raise RuntimeError("worker capabilities do not produce any runnable task lanes")
    parallel_model_key = args.parallel_model_key
    parallel_lane_allowance = _parallel_lane_allowance(
        getattr(args, "parallel_model_lanes", ""), model_keys
    )
    if args.model_concurrency > 1 and not parallel_lane_allowance:
        if not parallel_model_key:
            raise RuntimeError("parallel model lanes require one exact --parallel-model-key")
        if parallel_model_key not in model_keys:
            raise RuntimeError("parallel model key must be a configured model key")
    accepted_queues = sorted({route for _, routes in lanes for route in routes})
    adaptive_queue: _AdaptiveAppleQueue | None = None
    if args.adaptive_final_queue_threshold > 0:
        if not {"small-vlm", "medium-vlm"}.issubset(set(accepted_queues)):
            raise RuntimeError(
                "adaptive final-review scheduling requires small-vlm and medium-vlm routes"
            )
        if len(model_keys) < 2:
            raise RuntimeError("adaptive final-review scheduling requires two configured models")
        adaptive_queue = _AdaptiveAppleQueue(args.adaptive_final_queue_threshold)
    maximum_concurrency = len(lanes)
    model_lane_count = sum(
        1 for _name, lane_routes in lanes if any("vlm" in route for route in lane_routes)
    )
    active_jobs: set[str] = set()
    resident_model_keys: set[str] = set()
    model_residency = ModelResidencyManager(resident_model_keys)
    available_model_keys: set[str] = set()
    runtime_changed = asyncio.Event()
    runtime_activity: dict[str, object] = {
        "runtime_phase": "idle",
        "runtime_model_key": None,
        "runtime_phase_started_at": None,
        "quiet_mode_active": bool(quiet_hours and quiet_hours.active()),
        "quiet_mode_start": args.quiet_hours_start,
        "quiet_mode_end": args.quiet_hours_end,
        "quiet_mode_timezone": args.quiet_hours_timezone,
        "quiet_mode_cooldown_seconds": args.quiet_hours_cooldown_seconds,
        "quiet_mode_performance_percent": args.quiet_hours_performance_percent,
        "parallel_model_key": parallel_model_key,
        "model_load_context_length": args.model_load_context_length,
        "model_load_parallel": args.model_load_parallel,
        "model_kv_cache_type": os.getenv("OLLAMA_KV_CACHE_TYPE"),
        "runtime_model_loaded_at": None,
    }
    host_current_load: dict[str, float] = {}
    model_runtime_routes = {model_key: args.model_provider for model_key in model_keys}
    if adviser_model_key:
        model_runtime_routes[adviser_model_key] = (
            getattr(args, "adviser_model_runtime_name", None) or "mlx"
        )

    def heartbeat_payload() -> dict[str, Any]:
        return _identity(
            args.hostname,
            capabilities,
            machine_group=args.machine_group,
            accepted_queues=accepted_queues,
            maximum_concurrency=maximum_concurrency,
            active_jobs=len(active_jobs),
            available_models=sorted(available_model_keys),
            model_runtime_routes={
                model_key: runtime
                for model_key, runtime in model_runtime_routes.items()
                if model_key in available_model_keys
            },
            storage_path=token_path.parent,
            runtime_details={
                "model_lane_count": model_lane_count,
                "runtime_session_id": str(runtime_session_id),
                "runtime_session_started_at": runtime_session_started_at.isoformat(),
                "resident_model_keys": sorted(resident_model_keys),
                "model_idle_ttl_seconds": args.model_idle_ttl_seconds if provider else 0,
                **runtime_activity,
                "host_current_load": dict(host_current_load),
                **power_status.runtime_details(
                    require_ac_power=args.require_ac_power,
                    minimum_battery=args.minimum_battery,
                    battery_resume_percent=args.battery_resume_percent,
                    heavy_task_cooldown_seconds=args.heavy_task_cooldown_seconds,
                ),
                **(adaptive_queue.runtime_details() if adaptive_queue is not None else {}),
            },
        )

    stop = asyncio.Event()
    restart_requested = asyncio.Event()
    runtime_controls = _WorkerRuntimeControls(
        adaptive_queue=adaptive_queue,
    )
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop.set)
    try:
        if power_status_provider is not None:
            await _refresh_host_power_status(
                power_status_provider,
                power_status,
                runtime_changed,
                host_current_load,
            )
        async with httpx.AsyncClient(
            base_url=args.server.rstrip("/"),
            verify=_ssl_context(Path(args.ca_file) if args.ca_file else None),
            timeout=httpx.Timeout(30, read=180),
            # Keep idle coordinator connections warm across the 5s lane polls;
            # the httpx default 5s keepalive would re-handshake every poll.
            limits=httpx.Limits(keepalive_expiry=60.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            heartbeat_value = await _establish_worker_session(
                client,
                token_path=token_path,
                identity_path=identity_path,
                automatic_enrollment=args.automatic_enrollment,
                heartbeat_payload=heartbeat_payload,
                stop=stop,
                retry_seconds=max(5.0, min(30.0, args.heartbeat_seconds)),
            )
            if heartbeat_value is None:
                return
            runtime_controls.apply_response(heartbeat_value)
            initial_control_revision = heartbeat_value.get("control_revision", 0)
            if not isinstance(initial_control_revision, int) or isinstance(
                initial_control_revision, bool
            ):
                initial_control_revision = 0
            logger.info(
                "worker supervisor ready",
                extra={
                    "hostname": args.hostname,
                    "maximum_concurrency": maximum_concurrency,
                    "lanes": [name for name, _ in lanes],
                },
            )
            heartbeat_task = asyncio.create_task(
                _run_worker_heartbeat(
                    client,
                    token_path=token_path,
                    identity_path=identity_path,
                    automatic_enrollment=args.automatic_enrollment,
                    heartbeat_payload=heartbeat_payload,
                    stop=stop,
                    restart_requested=restart_requested,
                    initial_control_revision=initial_control_revision,
                    interval_seconds=args.heartbeat_seconds,
                    runtime_controls=runtime_controls,
                    runtime_changed=runtime_changed,
                )
            )
            inventory_task = (
                asyncio.create_task(
                    _run_model_inventory_sync(
                        client,
                        provider=provider,
                        stop=stop,
                        available_model_keys=available_model_keys,
                        runtime_changed=runtime_changed,
                    )
                )
                if provider is not None
                else None
            )
            power_status_task = (
                asyncio.create_task(
                    _run_host_power_sync(
                        power_status_provider,
                        power_status,
                        runtime_changed,
                        stop,
                        host_current_load,
                    )
                )
                if power_status_provider is not None
                else None
            )
            lane_tasks = [
                asyncio.create_task(
                    _run_lane(
                        client,
                        lane_name=lane_name,
                        routes=routes,
                        capabilities=accepted,
                        provider=provider,
                        stop=stop,
                        active_jobs=active_jobs,
                        poll_seconds=args.poll_seconds,
                        require_ac_power=args.require_ac_power,
                        minimum_battery=args.minimum_battery,
                        battery_resume_percent=args.battery_resume_percent,
                        power_status=power_status if power_status_provider is not None else None,
                        heavy_task_cooldown_seconds=args.heavy_task_cooldown_seconds,
                        quiet_hours=quiet_hours,
                        model_keys=available_model_keys,
                        lane_model_keys=parallel_lane_model_keys(
                            lane_name,
                            allowance=parallel_lane_allowance,
                            fallback=parallel_model_key,
                        ),
                        residency=model_residency,
                        model_idle_ttl_seconds=args.model_idle_ttl_seconds,
                        runtime_activity=runtime_activity if provider is not None else None,
                        runtime_changed=runtime_changed if provider is not None else None,
                        adaptive_queue=(
                            adaptive_queue
                            if {"small-vlm", "medium-vlm"}.issubset(set(routes))
                            else None
                        ),
                        runtime_controls=runtime_controls,
                    )
                )
                for lane_name, routes in lanes
            ]
            supervised_tasks = [heartbeat_task, *lane_tasks]
            if inventory_task is not None:
                supervised_tasks.append(inventory_task)
            if power_status_task is not None:
                supervised_tasks.append(power_status_task)
            try:
                await _wait_for_worker_supervisor(stop, supervised_tasks)
            finally:
                stop.set()
                with contextlib.suppress(httpx.HTTPError):
                    await client.post("/api/v1/workers/drain")
                await asyncio.gather(*lane_tasks, return_exceptions=True)
                heartbeat_task.cancel()
                background_tasks = [heartbeat_task]
                if inventory_task is not None:
                    inventory_task.cancel()
                    background_tasks.append(inventory_task)
                if power_status_task is not None:
                    power_status_task.cancel()
                    background_tasks.append(power_status_task)
                await asyncio.gather(*background_tasks, return_exceptions=True)
        logger.info(
            "worker agent stopped cleanly",
            extra={
                "hostname": args.hostname,
                "restart_requested": restart_requested.is_set(),
            },
        )
    finally:
        if provider is not None:
            for model_key in tuple(resident_model_keys):
                with contextlib.suppress(LocalInferenceError, ValueError):
                    await provider.unload(model_key)
            await provider.close()
        if power_status_provider is not None:
            await power_status_provider.close()
