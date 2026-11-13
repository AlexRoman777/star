from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from datetime import time as wall_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from star.agent._base import TECHNICAL_CAPABILITIES, logger
from star.agent.identity import (
    PowerAdmissionState,
    _automatically_enroll,
    _can_recover_worker_credential,
    _power_policy_allows_work,
    _read_optional_file,
    _rotate_credential_if_needed,
    _submit_failure,
)
from star.agent.inference_tasks import _process_task
from star.inference.policy import LocalInferenceError
from star.inference.provider import (
    LocalInferenceProvider,
)


@dataclass
class _WorkerRuntimeControls:
    adaptive_queue: _AdaptiveAppleQueue | None = None
    model_concurrency_limit: int = 6

    def apply_response(self, payload: dict[str, Any]) -> None:
        # helper_active is the schedule-masked effective value; older
        # coordinators send only the raw toggle.
        helper_enabled = payload.get("helper_active", payload.get("helper_mode_enabled"))
        if isinstance(helper_enabled, bool) and self.adaptive_queue is not None:
            self.adaptive_queue.set_helper_enabled(helper_enabled)
        if self.adaptive_queue is not None and helper_enabled is None:
            # v255 coordinators send no toggle: help is automatic.
            self.adaptive_queue.set_helper_enabled(True)
        lane_limit = payload.get("model_concurrency_limit")
        if isinstance(lane_limit, int) and not isinstance(lane_limit, bool):
            self.model_concurrency_limit = min(6, max(1, lane_limit))

    def model_lane_enabled(self, lane_name: str) -> bool:
        if not lane_name.startswith("model-"):
            return True
        try:
            lane_number = int(lane_name.removeprefix("model-"))
        except ValueError:
            return True
        return lane_number <= self.model_concurrency_limit


@dataclass(frozen=True)
class QuietHoursPolicy:
    timezone: ZoneInfo
    start: wall_time
    end: wall_time
    cooldown_seconds: float
    performance_percent: int = 100

    @classmethod
    def parse(
        cls,
        *,
        timezone: str,
        start: str,
        end: str,
        cooldown_seconds: float,
        performance_percent: int = 100,
    ) -> QuietHoursPolicy:
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("quiet-hours timezone is unavailable") from exc
        try:
            start_time = wall_time.fromisoformat(start)
            end_time = wall_time.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("quiet hours must use HH:MM") from exc
        if not 0 <= cooldown_seconds <= 3600:
            raise ValueError("quiet-hours cooldown must be between 0 and 3600 seconds")
        if not 1 <= performance_percent <= 100:
            raise ValueError("quiet-hours performance must be between 1 and 100 percent")
        return cls(
            timezone=zone,
            start=start_time,
            end=end_time,
            cooldown_seconds=cooldown_seconds,
            performance_percent=performance_percent,
        )

    def active(self, current: datetime | None = None) -> bool:
        local = (current or datetime.now(UTC)).astimezone(self.timezone).time().replace(tzinfo=None)
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end

    def cooldown_for(self, work_seconds: float) -> float:
        """Return a safe post-job rest approximating the configured duty cycle.

        Star cannot safely rewrite a host GPU power limit from an
        unprivileged worker.  Matching rest to work duration gives a truthful
        approximately 50 percent wall-clock duty cycle while preserving the
        exact model, prompt, input, and sampling contract.
        """

        bounded_work_seconds = max(0.0, work_seconds)
        duty_cycle_rest = bounded_work_seconds * (
            (100 - self.performance_percent) / self.performance_percent
        )
        return min(3600.0, max(self.cooldown_seconds, duty_cycle_rest))


class _AdaptiveAppleQueue:
    """Switch one Apple accelerator between primary help and batched Qwen work.

    The owner's order is primary first: while primary help is enabled the lane
    asks for Ministral work before senior work and only turns to the senior
    queue when the coordinator has no primary photo to offer. Senior work is
    therefore drained in the gaps, never ahead of the primary tier.
    """

    def __init__(self, threshold: int) -> None:
        if not 2 <= threshold <= 10_000:
            raise ValueError("adaptive final-review threshold must be between 2 and 10000")
        self.threshold = threshold
        self.final_mode = False
        self.helper_enabled = False
        self.senior_first = False

    def set_helper_enabled(self, enabled: bool) -> None:
        self.helper_enabled = enabled

    def set_senior_first(self, enabled: bool) -> None:
        self.senior_first = enabled

    def offers(self) -> list[tuple[int | None, list[str]]]:
        senior: tuple[int | None, list[str]] = (None, ["medium-vlm"])
        primary: tuple[int | None, list[str]] = (None, ["small-vlm"])
        if not self.helper_enabled:
            return [senior]
        return [senior, primary] if self.senior_first else [primary, senior]

    def minimum_depth(self, routes: list[str]) -> int:
        return 1  # A single queued photo on either route is worth taking.

    def record_task(self, routes: list[str]) -> None:
        self.final_mode = "medium-vlm" in routes

    def record_unavailable(self, routes: list[str]) -> None:
        if "medium-vlm" in routes:
            self.final_mode = False

    def runtime_details(self) -> dict[str, object]:
        return {
            "adaptive_model_mode": "qwen-final" if self.final_mode else "ministral-helper",
            "primary_help_enabled": self.helper_enabled,
            "qwen_switch_threshold": 1,
            "switch_policy": "senior-first" if self.senior_first else "primary-first",
        }


_HEALTH_CACHE_TTL_SECONDS = 15.0
_health_cache: dict[str, tuple[float, bool]] = {}


async def _cached_health(key: str, probe: Callable[[], Awaitable[bool]]) -> bool:
    """Memoize runtime health briefly so idle polls stop hammering local runtimes."""

    cached = _health_cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _HEALTH_CACHE_TTL_SECONDS:
        return cached[1]
    healthy = await probe()
    _health_cache[key] = (time.monotonic(), healthy)
    return healthy


async def _route_group_is_healthy(
    routes: list[str],
    *,
    provider: LocalInferenceProvider | None,
) -> bool:
    if any("vlm" in route for route in routes):
        if provider is None:
            return False
        return await _cached_health("model-runtime", provider.health)
    return True


async def _lease_from_route_offers(
    client: httpx.AsyncClient,
    *,
    lane_name: str,
    routes: list[str],
    capabilities: list[str],
    model_keys: Collection[str],
    lane_model_keys: Collection[str] | None = None,
    provider: LocalInferenceProvider | None,
    adaptive_queue: _AdaptiveAppleQueue | None = None,
    residency: ModelResidencyManager | None = None,
    runtime_activity: dict[str, object] | None = None,
    runtime_changed: asyncio.Event | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    offers: list[tuple[int | None, list[str]]] = []
    if adaptive_queue is not None:
        offers.extend(adaptive_queue.offers())
    else:
        offers.append((None, routes))
    found_healthy_runtime = False
    for _group_index, offered_routes in offers:
        if not await _route_group_is_healthy(offered_routes, provider=provider):
            logger.warning(
                "local accelerator runtime is unavailable; trying another route",
                extra={"lane": lane_name, "routes": offered_routes},
            )
            continue
        found_healthy_runtime = True
        uses_model = any("vlm" in route for route in offered_routes)
        accepted_model_keys = set(model_keys)
        if lane_model_keys is not None:
            accepted_model_keys.intersection_update(lane_model_keys)
        if uses_model and not accepted_model_keys:
            continue
        lease = await client.post(
            "/api/v1/workers/tasks/lease",
            json={
                "accepted_capabilities": capabilities,
                "accepted_routing_keys": offered_routes,
                "accepted_model_keys": sorted(accepted_model_keys) if uses_model else [],
                "minimum_matching_queue_depth": (
                    adaptive_queue.minimum_depth(offered_routes)
                    if adaptive_queue is not None
                    else 1
                ),
                "urgent_deep_review_bypass": bool(
                    adaptive_queue is not None and "medium-vlm" in offered_routes
                ),
            },
        )
        if lease.status_code == 204:
            refusal = lease.headers.get("X-Star-Lease-Refusal")
            if refusal:
                logger.info(
                    "coordinator refused a lane poll",
                    extra={"lane": lane_name, "routes": offered_routes, "reason": refusal},
                )
            if (
                lease.headers.get("X-Star-Unload-Idle") == "1"
                and provider is not None
                and residency is not None
            ):
                unloaded = await residency.unload_idle(provider, idle_ttl_seconds=0)
                if unloaded is not None:
                    _set_runtime_phase(
                        runtime_activity, runtime_changed, phase="idle", model_key=None
                    )
                    # The coordinator refused only because the previous model
                    # was still resident. Now that it is gone, ask again for the
                    # same work before falling through to a lower-priority route;
                    # otherwise the next offer reloads the old model and the
                    # owner's order never takes effect.
                    lease = await client.post(
                        "/api/v1/workers/tasks/lease",
                        json={
                            "accepted_capabilities": capabilities,
                            "accepted_routing_keys": offered_routes,
                            "accepted_model_keys": (
                                sorted(accepted_model_keys) if uses_model else []
                            ),
                            "minimum_matching_queue_depth": (
                                adaptive_queue.minimum_depth(offered_routes)
                                if adaptive_queue is not None
                                else 1
                            ),
                            "urgent_deep_review_bypass": bool(
                                adaptive_queue is not None and "medium-vlm" in offered_routes
                            ),
                        },
                    )
                    if lease.status_code != 204:
                        lease.raise_for_status()
                        task = lease.json()
                        if adaptive_queue is not None:
                            adaptive_queue.record_task(offered_routes)
                        return task, found_healthy_runtime
            if adaptive_queue is not None:
                adaptive_queue.record_unavailable(offered_routes)
            continue
        lease.raise_for_status()
        task = lease.json()
        if adaptive_queue is not None:
            adaptive_queue.record_task(offered_routes)
        return task, found_healthy_runtime
    return None, found_healthy_runtime


def _lane_routes(
    capabilities: set[str],
    *,
    technical_concurrency: int,
    model_concurrency: int,
) -> list[tuple[str, list[str]]]:
    lanes: list[tuple[str, list[str]]] = []
    if capabilities.intersection(TECHNICAL_CAPABILITIES):
        lanes.extend(
            (f"technical-{index + 1}", ["technical"]) for index in range(technical_concurrency)
        )
    if "semantic-embedding" in capabilities:
        lanes.append(("semantic-embedding-1", ["semantic-embedding"]))
    model_routes: list[str] = []
    if "small-vlm" in capabilities:
        model_routes.append("small-vlm")
    if capabilities.intersection({"large-vlm", "medium-vlm", "mlx", "multi-image-review"}):
        model_routes.append("medium-vlm")
    if model_routes:
        lanes.extend((f"model-{index + 1}", model_routes) for index in range(model_concurrency))
    return [(name, routes) for name, routes in lanes if routes]


async def _wait_for_poll(stop: asyncio.Event, seconds: float) -> None:
    # Jitter de-synchronizes lanes and workers after a coordinator restart so
    # the heavy lease path never receives a lockstep thundering herd.
    jittered = seconds * random.uniform(0.8, 1.25) if seconds > 0 else seconds  # noqa: S311 - scheduling jitter, not cryptography
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=jittered)


def _set_runtime_phase(
    runtime_activity: dict[str, object] | None,
    runtime_changed: asyncio.Event | None,
    *,
    phase: str,
    model_key: str | None,
) -> None:
    if runtime_activity is None:
        return
    if (
        runtime_activity.get("runtime_phase") == phase
        and runtime_activity.get("runtime_model_key") == model_key
    ):
        return
    runtime_activity.update(
        {
            "runtime_phase": phase,
            "runtime_model_key": model_key,
            "runtime_phase_started_at": datetime.now(UTC).isoformat(),
        }
    )
    if runtime_changed is not None:
        runtime_changed.set()


# A resident model keeps the lane for at least this long before lane one offers
# the coordinator the other approved models. Mirrors the coordinator's
# MODEL_TURN_MINIMUM; the coordinator alone decides whether a turn ends, and it
# does so only when work for another model has waited MODEL_TURN_STARVATION.
# The coordinator decides model turns from measured backlog and switch cost
# (services.model_turns); lane one always offers every model it serves.
MODEL_TURN_MINIMUM_SECONDS = 0.0


def parallel_lane_model_keys(
    lane_name: str, *, allowance: dict[str, int], fallback: str | None
) -> set[str] | None:
    """Which models a parallel lane may offer, by measured lane capacity.

    Lane one always chooses and loads; it has no restriction. Every other lane
    may only offer a model whose measured lane allowance reaches it, because
    what fits in parallel depends on the model: six Ministral lanes were the
    Mac's limit, and four Qwen3-VL comparison lanes, where the fifth and
    sixth bought no throughput and grew swap (v267). Without an allowance the
    old single-key rule applies, so every other machine is unaffected.
    """

    if not lane_name.startswith("model-") or lane_name == "model-1":
        return None
    try:
        lane_number = int(lane_name.removeprefix("model-"))
    except ValueError:
        return None
    if not allowance:
        return {fallback} if fallback is not None else None
    return {key for key, lanes in allowance.items() if lanes >= lane_number}


def _eligible_model_keys_for_lane(
    model_keys: Collection[str],
    *,
    lane_model_keys: Collection[str] | None,
    loaded_model_key: str | None,
    turn_may_end: bool = False,
) -> set[str]:
    """Keep extra parallel lanes behind an already resident approved model.

    Lane one chooses and loads the next model. Extra lanes never race that
    transition: they become eligible only after their one exact model identity
    is resident, then submit work for that identity alone.

    Lane one offered only the loaded model too, so the coordinator never saw
    this machine as able to take the other model's work and its bounded model
    turn could never begin: comparisons waited two hours beside a full primary
    queue and ran only after a restart. Once the resident model has held the
    lane for its minimum and no lane is mid-task, lane one offers every
    approved model and lets the coordinator choose.
    """

    eligible = set(model_keys)
    if lane_model_keys is not None:
        allowed = set(lane_model_keys)
        if loaded_model_key not in allowed:
            return set()
        eligible.intersection_update(allowed)
        if loaded_model_key is not None:
            eligible.intersection_update({loaded_model_key})
        return eligible
    if loaded_model_key is not None and not turn_may_end:
        eligible.intersection_update({loaded_model_key})
    return eligible


async def _run_lane(
    client: httpx.AsyncClient,
    *,
    lane_name: str,
    routes: list[str],
    capabilities: list[str],
    provider: LocalInferenceProvider | None,
    stop: asyncio.Event,
    active_jobs: set[str],
    poll_seconds: float,
    require_ac_power: bool,
    minimum_battery: int,
    battery_resume_percent: int,
    power_status: PowerAdmissionState | None,
    heavy_task_cooldown_seconds: float,
    quiet_hours: QuietHoursPolicy | None,
    model_keys: Collection[str],
    lane_model_keys: Collection[str] | None = None,
    residency: ModelResidencyManager | None = None,
    model_idle_ttl_seconds: int = 30,
    runtime_activity: dict[str, object] | None = None,
    runtime_changed: asyncio.Event | None = None,
    adaptive_queue: _AdaptiveAppleQueue | None = None,
    runtime_controls: _WorkerRuntimeControls | None = None,
) -> None:
    residency = residency if residency is not None else ModelResidencyManager(set())
    while not stop.is_set():
        if runtime_controls is not None and not runtime_controls.model_lane_enabled(lane_name):
            await _wait_for_poll(stop, poll_seconds)
            continue
        quiet_mode_active = bool(quiet_hours and quiet_hours.active())
        if (
            runtime_activity is not None
            and runtime_activity.get("quiet_mode_active") != quiet_mode_active
        ):
            runtime_activity["quiet_mode_active"] = quiet_mode_active
            if runtime_changed is not None:
                runtime_changed.set()
        if provider is not None and residency.loaded_model_key is not None:
            idle_candidate = residency.loaded_model_key
            try:
                unloaded = await residency.unload_idle(
                    provider,
                    idle_ttl_seconds=model_idle_ttl_seconds,
                )
            except LocalInferenceError:
                _set_runtime_phase(
                    runtime_activity,
                    runtime_changed,
                    phase="error",
                    model_key=idle_candidate,
                )
                logger.warning(
                    "idle local model unload failed; the model stays resident",
                    extra={"lane": lane_name, "model_key": idle_candidate},
                )
            else:
                if unloaded is not None:
                    _set_runtime_phase(
                        runtime_activity,
                        runtime_changed,
                        phase="idle",
                        model_key=None,
                    )
        if not await _power_policy_allows_work(
            require_ac_power=require_ac_power,
            minimum_battery=minimum_battery,
            battery_resume_percent=battery_resume_percent,
            power_status=power_status,
        ):
            await _wait_for_poll(stop, poll_seconds)
            continue
        try:
            effective_model_keys = _eligible_model_keys_for_lane(
                model_keys,
                lane_model_keys=lane_model_keys,
                loaded_model_key=residency.loaded_model_key,
                turn_may_end=residency.turn_may_end(MODEL_TURN_MINIMUM_SECONDS),
            )
            if lane_model_keys is not None and not effective_model_keys:
                await _wait_for_poll(stop, poll_seconds)
                continue
            task, found_healthy_runtime = await _lease_from_route_offers(
                client,
                lane_name=lane_name,
                routes=routes,
                capabilities=capabilities,
                model_keys=effective_model_keys,
                lane_model_keys=lane_model_keys,
                provider=provider,
                adaptive_queue=adaptive_queue,
                residency=residency,
                runtime_activity=runtime_activity,
                runtime_changed=runtime_changed,
            )
            if task is None:
                await _wait_for_poll(
                    stop,
                    poll_seconds if found_healthy_runtime else max(15.0, poll_seconds),
                )
                continue
            task_id = str(task["task_id"])
            target_ref = task.get("target_ref")
            target_ref = target_ref if isinstance(target_ref, dict) else {}
            target_model_key = target_ref.get("model_key")
            target_model_key = target_model_key if isinstance(target_model_key, str) else None
            active_jobs.add(task_id)
            task_started_at = time.monotonic()
            logger.info(
                "worker lane acquired task",
                extra={"lane": lane_name, "task_id": task_id, "routing_key": task["routing_key"]},
            )
            model_acquired = False
            try:
                if provider is not None and target_model_key is not None:
                    loaded = residency.loaded_model_key
                    transition_phase = (
                        "loading"
                        if loaded is None
                        else "switching"
                        if loaded != target_model_key
                        else "running"
                    )
                    _set_runtime_phase(
                        runtime_activity,
                        runtime_changed,
                        phase=transition_phase,
                        model_key=target_model_key,
                    )
                    try:
                        # Acquire advertises residency before inference triggers
                        # the managed load, so residency reporting stays truthful
                        # through load, inference, and the idle interval.
                        async with asyncio.timeout(60):
                            await residency.acquire(
                                provider,
                                target_model_key=target_model_key,
                                model_keys=model_keys,
                            )
                    except LocalInferenceError, ValueError, TimeoutError:
                        _set_runtime_phase(
                            runtime_activity,
                            runtime_changed,
                            phase="error",
                            model_key=target_model_key,
                        )
                        logger.exception(
                            "local model transition failed before inference",
                            extra={"lane": lane_name, "task_id": task_id},
                        )
                        await _submit_failure(
                            client,
                            task,
                            "ModelTransitionFailed",
                            retryable=True,
                        )
                        continue
                    model_acquired = True
                    if loaded != target_model_key and runtime_activity is not None:
                        runtime_activity["runtime_model_loaded_at"] = datetime.now(UTC).isoformat()
                        if runtime_changed is not None:
                            runtime_changed.set()
                await _process_task(client, task, provider)
            finally:
                if model_acquired:
                    residency.release()
                active_jobs.discard(task_id)
                if target_model_key is not None and residency.is_resident(target_model_key):
                    _set_runtime_phase(
                        runtime_activity,
                        runtime_changed,
                        phase="resident",
                        model_key=target_model_key,
                    )
            cooldown_seconds = heavy_task_cooldown_seconds
            if quiet_hours is not None and quiet_hours.active():
                cooldown_seconds = quiet_hours.cooldown_for(time.monotonic() - task_started_at)
            power_managed_task = target_model_key is not None
            if power_managed_task and cooldown_seconds > 0:
                _set_runtime_phase(
                    runtime_activity,
                    runtime_changed,
                    phase=(
                        "quiet_cooldown"
                        if quiet_hours is not None and quiet_hours.active()
                        else "cooldown"
                    ),
                    model_key=target_model_key,
                )
                await _wait_for_poll(stop, cooldown_seconds)
        except httpx.RequestError:
            if stop.is_set():
                return
            logger.warning(
                "worker lane connection unavailable; retrying",
                extra={"lane": lane_name},
            )
            await _wait_for_poll(stop, max(5.0, poll_seconds))
        except httpx.HTTPStatusError as exc:
            if stop.is_set():
                return
            status_code = exc.response.status_code
            logger.warning(
                (
                    "coordinator temporarily rejected worker lane; retrying"
                    if status_code >= 500
                    else "worker lane policy rejected request; retrying"
                ),
                extra={"lane": lane_name, "status_code": status_code},
            )
            await _wait_for_poll(
                stop,
                max(15.0 if status_code >= 500 else 30.0, poll_seconds),
            )


async def _prepare_model_residency(
    provider: LocalInferenceProvider,
    *,
    active_model_key: str | None,
    target_model_key: str,
    model_keys: Collection[str],
) -> None:
    """Evict incompatible runtime state before a visual model request.

    Ollama outlives the worker process, while ``active_model_key`` does not.
    On the first post-restart task, evict every other approved model instead of
    assuming the GPU starts empty.  Later transitions need evict only the model
    this process knows it made resident.
    """

    if active_model_key is None:
        for model_key in dict.fromkeys(model_keys):
            if model_key != target_model_key:
                await provider.unload(model_key)
    elif active_model_key != target_model_key:
        await provider.unload(active_model_key)


class LocalModelBusyError(LocalInferenceError):
    pass


class ModelResidencyManager:
    """Process-level ownership of the single loaded local model.

    With more than one model lane serving parallel requests against one loaded
    model, per-lane residency bookkeeping would evict a model that a sibling
    lane is actively using, or falsely clear the residency the coordinator
    reports on the Machines page. This manager refcounts in-flight users per
    model key so unloads happen only when no lane is using the model.
    """

    def __init__(self, resident_models: set[str]) -> None:
        self._transition_lock = asyncio.Lock()
        self._active_users = 0
        self._loaded: str | None = None
        self._loaded_since: float | None = None
        self._last_used: float | None = None
        self._resident_models = resident_models
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def loaded_model_key(self) -> str | None:
        return self._loaded

    def is_resident(self, model_key: str) -> bool:
        return model_key in self._resident_models

    def turn_may_end(self, minimum_seconds: float) -> bool:
        """Whether lane one may offer the coordinator another model right now.

        True with nothing loaded; otherwise only once the loaded model has held
        the lane for the minimum and no lane is mid-task, so a switch the
        coordinator asks for never waits on a running inference.
        """

        if self._loaded is None or self._loaded_since is None:
            return True
        if self._active_users > 0:
            return False
        return time.monotonic() - self._loaded_since >= minimum_seconds

    async def acquire(
        self,
        provider: LocalInferenceProvider,
        *,
        target_model_key: str,
        model_keys: Collection[str],
    ) -> None:
        async with self._transition_lock:
            if self._loaded != target_model_key and self._active_users > 0:
                try:
                    await asyncio.wait_for(self._idle.wait(), timeout=5)
                except TimeoutError as exc:
                    raise LocalModelBusyError("resident model is busy in another lane") from exc
            await _prepare_model_residency(
                provider,
                active_model_key=self._loaded,
                target_model_key=target_model_key,
                model_keys=model_keys,
            )
            if self._loaded is not None and self._loaded != target_model_key:
                self._resident_models.discard(self._loaded)
            if self._loaded != target_model_key:
                self._loaded_since = time.monotonic()
            self._loaded = target_model_key
            self._resident_models.add(target_model_key)
            self._active_users += 1
            self._idle.clear()
            self._last_used = time.monotonic()

    def release(self) -> None:
        self._active_users = max(0, self._active_users - 1)
        if self._active_users == 0:
            self._idle.set()
        self._last_used = time.monotonic()

    async def unload_idle(
        self,
        provider: LocalInferenceProvider,
        *,
        idle_ttl_seconds: float,
    ) -> str | None:
        """Unload the loaded model only when idle past the TTL with no users."""

        async with self._transition_lock:
            if (
                self._loaded is None
                or self._active_users > 0
                or self._last_used is None
                or time.monotonic() - self._last_used < idle_ttl_seconds
            ):
                return None
            unloaded = self._loaded
            await provider.unload(unloaded)
            self._resident_models.discard(unloaded)
            self._loaded = None
            self._loaded_since = None
            self._last_used = None
            return unloaded


async def _run_worker_heartbeat(
    client: httpx.AsyncClient,
    *,
    token_path: Path,
    identity_path: Path,
    automatic_enrollment: bool,
    heartbeat_payload: Callable[[], dict[str, Any]],
    stop: asyncio.Event,
    restart_requested: asyncio.Event,
    initial_control_revision: int,
    interval_seconds: float,
    runtime_controls: _WorkerRuntimeControls | None = None,
    runtime_changed: asyncio.Event | None = None,
) -> None:
    recovery_failures = 0
    recovery_not_before = 0.0
    transient_failures = 0
    while not stop.is_set():
        if runtime_changed is None:
            await _wait_for_poll(stop, interval_seconds)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(runtime_changed.wait(), timeout=interval_seconds)
            runtime_changed.clear()
        if stop.is_set():
            return
        if time.monotonic() < recovery_not_before:
            continue
        try:
            response = await client.post(
                "/api/v1/workers/heartbeat",
                json=heartbeat_payload(),
            )
            response.raise_for_status()
            response_value = response.json()
            if runtime_controls is not None and isinstance(response_value, dict):
                runtime_controls.apply_response(response_value)
            await _rotate_credential_if_needed(client, token_path)
        except httpx.RequestError:
            # Suspend/resume commonly invalidates pooled sockets before the
            # host network is usable again. Keep the supervisor alive and let
            # the next heartbeat establish a fresh connection.
            transient_failures += 1
            if transient_failures == 1 or transient_failures & (transient_failures - 1) == 0:
                logger.warning(
                    "worker heartbeat connection is unavailable; retrying",
                    extra={"consecutive_failures": transient_failures},
                )
            continue
        except json.JSONDecodeError:
            transient_failures += 1
            if transient_failures == 1 or transient_failures & (transient_failures - 1) == 0:
                logger.warning(
                    "worker heartbeat response is invalid; retrying",
                    extra={"consecutive_failures": transient_failures},
                )
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                transient_failures += 1
                if transient_failures == 1 or transient_failures & (transient_failures - 1) == 0:
                    logger.warning(
                        "coordinator temporarily rejected worker heartbeat; retrying",
                        extra={
                            "status_code": exc.response.status_code,
                            "consecutive_failures": transient_failures,
                        },
                    )
                continue
            if automatic_enrollment and _can_recover_worker_credential(exc.response):
                try:
                    await _automatically_enroll(
                        client,
                        token_path=token_path,
                        identity_path=identity_path,
                        heartbeat=heartbeat_payload(),
                    )
                except httpx.RequestError, httpx.HTTPStatusError, ValueError:
                    recovery_failures += 1
                    recovery_not_before = time.monotonic() + min(
                        300.0,
                        max(interval_seconds, 1.0) * (2 ** min(recovery_failures, 6)),
                    )
                    if recovery_failures == 1 or recovery_failures & (recovery_failures - 1) == 0:
                        logger.warning(
                            "worker credential recovery is backing off",
                            extra={
                                "status_code": exc.response.status_code,
                                "consecutive_failures": recovery_failures,
                                "retry_after_seconds": round(
                                    recovery_not_before - time.monotonic(), 1
                                ),
                            },
                        )
                else:
                    recovery_failures = 0
                    recovery_not_before = 0.0
                    logger.info("worker credential recovered without restarting")
                continue
            # Authentication and policy failures require the normal process
            # startup path, which can recover or replace the credential.
            logger.error(
                "worker heartbeat policy rejected request; restarting worker",
                extra={"status_code": exc.response.status_code},
            )
            restart_requested.set()
            stop.set()
            return
        transient_failures = 0
        control_revision = response_value.get("control_revision", initial_control_revision)
        if (
            isinstance(control_revision, int)
            and not isinstance(control_revision, bool)
            and control_revision > initial_control_revision
        ):
            logger.info("coordinator requested a worker restart")
            restart_requested.set()
            stop.set()
            return


async def _establish_worker_session(
    client: httpx.AsyncClient,
    *,
    token_path: Path,
    identity_path: Path,
    automatic_enrollment: bool,
    heartbeat_payload: Callable[[], dict[str, Any]],
    stop: asyncio.Event,
    retry_seconds: float,
) -> dict[str, Any] | None:
    """Wait for the coordinator without turning an outage into a restart loop."""

    failures = 0
    while not stop.is_set():
        try:
            credential_token = await asyncio.to_thread(_read_optional_file, token_path)
            if credential_token:
                client.headers["Authorization"] = "Bearer " + credential_token
            elif automatic_enrollment:
                await _automatically_enroll(
                    client,
                    token_path=token_path,
                    identity_path=identity_path,
                    heartbeat=heartbeat_payload(),
                )
            else:
                raise RuntimeError(
                    "worker credential is missing and automatic enrollment is disabled"
                )
            try:
                await _rotate_credential_if_needed(client, token_path)
                heartbeat = await client.post(
                    "/api/v1/workers/heartbeat",
                    json=heartbeat_payload(),
                )
                heartbeat.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if not automatic_enrollment or not _can_recover_worker_credential(exc.response):
                    raise
                await _automatically_enroll(
                    client,
                    token_path=token_path,
                    identity_path=identity_path,
                    heartbeat=heartbeat_payload(),
                )
                heartbeat = await client.post(
                    "/api/v1/workers/heartbeat",
                    json=heartbeat_payload(),
                )
                heartbeat.raise_for_status()
            heartbeat_value = heartbeat.json()
            if not isinstance(heartbeat_value, dict):
                raise ValueError("worker heartbeat response must be an object")
            return heartbeat_value
        except httpx.RequestError:
            failures += 1
            if failures == 1 or failures & (failures - 1) == 0:
                logger.warning(
                    "worker startup connection is backing off",
                    extra={"consecutive_failures": failures},
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 and not automatic_enrollment:
                raise
            failures += 1
            if failures == 1 or failures & (failures - 1) == 0:
                logger.warning(
                    "worker startup authentication is backing off",
                    extra={
                        "status_code": exc.response.status_code,
                        "consecutive_failures": failures,
                    },
                )
        except json.JSONDecodeError, ValueError:
            failures += 1
            if failures == 1 or failures & (failures - 1) == 0:
                logger.warning(
                    "worker startup response is invalid; backing off",
                    extra={"consecutive_failures": failures},
                )
        delay = min(300.0, max(retry_seconds, 1.0) * (2 ** min(failures, 6)))
        await _wait_for_poll(stop, 0 if retry_seconds == 0 else delay)
    return None


async def _synchronize_model_inventory(
    client: httpx.AsyncClient,
    provider: LocalInferenceProvider,
) -> set[str]:
    inventory = await provider.inventory()
    if not inventory:
        return set()
    response = await client.post(
        "/api/v1/workers/models/discover",
        json={"models": [asdict(item) for item in inventory]},
    )
    response.raise_for_status()
    result = response.json()
    logger.info(
        "local model inventory synchronized",
        extra={
            "discovered": result.get("discovered", 0),
            "registered": result.get("registered", 0),
            "existing": result.get("existing", 0),
        },
    )
    return {item.model_key for item in inventory}


async def _run_model_inventory_sync(
    client: httpx.AsyncClient,
    *,
    provider: LocalInferenceProvider,
    stop: asyncio.Event,
    available_model_keys: set[str],
    runtime_changed: asyncio.Event,
    interval_seconds: float = 300.0,
) -> None:
    while not stop.is_set():
        try:
            discovered = await _synchronize_model_inventory(client, provider)
            if discovered != available_model_keys:
                available_model_keys.clear()
                available_model_keys.update(discovered)
                runtime_changed.set()
        except httpx.HTTPError, json.JSONDecodeError, AttributeError:
            logger.exception("local model inventory synchronization failed")
        await _wait_for_poll(stop, interval_seconds)


async def _wait_for_worker_supervisor(
    stop: asyncio.Event,
    supervised_tasks: list[asyncio.Task[None]],
) -> None:
    stop_waiter = asyncio.create_task(stop.wait(), name="worker-stop-waiter")
    try:
        done, _ = await asyncio.wait(
            [stop_waiter, *supervised_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter in done or stop.is_set():
            return
        completed = next(iter(done))
        await completed
        raise RuntimeError(f"worker subsystem stopped unexpectedly: {completed.get_name()}")
    finally:
        stop_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_waiter
