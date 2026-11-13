from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import platform
import secrets
import shutil
import ssl
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from star import __version__
from star.agent._base import (
    TASK_HEARTBEAT_FAILURE_LIMIT,
    TASK_HEARTBEAT_SECONDS,
    LeaseLostError,
    TaskCancelledError,
    logger,
)
from star.agent.capacity import _runtime_load
from star.domain.schemas import WorkerHeartbeat
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)
from star.services.worker_identity import automatic_enrollment_message


@dataclass(slots=True)
class PowerAdmissionState:
    """Host-reported power state with an explicit paused/full work boundary."""

    power_source: str = "unknown"
    battery_percent: int | None = None
    charging: bool | None = None
    charger_wattage: int | None = None
    waiting_for_recharge: bool = False
    updated_at: datetime | None = None

    def update(
        self,
        *,
        power_source: str,
        battery_percent: int | None,
        charging: bool | None,
        charger_wattage: int | None = None,
    ) -> None:
        self.power_source = power_source
        self.battery_percent = battery_percent
        self.charging = charging
        self.charger_wattage = charger_wattage
        self.updated_at = datetime.now(UTC)

    def work_mode(
        self,
        *,
        require_ac_power: bool,
        minimum_battery: int,
        battery_resume_percent: int | None = None,
    ) -> str:
        """Return the current power state without admitting work on missing telemetry.

        Once the battery reaches the floor, keep work paused until the separate
        resume threshold is reached. This hysteresis prevents repeated one-batch
        charge/drain loops when a weak charger cannot sustain inference.
        """

        if not require_ac_power and minimum_battery <= 0:
            self.waiting_for_recharge = False
            return "unrestricted"
        if self.power_source == "unknown" or self.battery_percent is None:
            self.waiting_for_recharge = True
            return "telemetry_unavailable"
        if require_ac_power and self.power_source != "ac":
            return "unplugged"
        resume_percent = max(minimum_battery, battery_resume_percent or minimum_battery)
        if self.battery_percent <= minimum_battery:
            self.waiting_for_recharge = True
            return "paused_low_battery"
        if self.waiting_for_recharge and self.battery_percent < resume_percent:
            return "charging_to_resume"
        self.waiting_for_recharge = False
        return "full"

    def allows_work(
        self,
        *,
        require_ac_power: bool,
        minimum_battery: int,
        battery_resume_percent: int | None = None,
    ) -> bool:
        return self.work_mode(
            require_ac_power=require_ac_power,
            minimum_battery=minimum_battery,
            battery_resume_percent=battery_resume_percent,
        ) in {"unrestricted", "full"}

    def runtime_details(
        self,
        *,
        require_ac_power: bool = False,
        minimum_battery: int = 0,
        battery_resume_percent: int | None = None,
        heavy_task_cooldown_seconds: float = 0,
    ) -> dict[str, object]:
        admission_state = self.work_mode(
            require_ac_power=require_ac_power,
            minimum_battery=minimum_battery,
            battery_resume_percent=battery_resume_percent,
        )
        return {
            "power_state": self.power_source,
            "battery_percent": self.battery_percent,
            "battery_charging": self.charging,
            "battery_charger_wattage": self.charger_wattage,
            "battery_admission_state": admission_state,
            "battery_pause_at_percent": minimum_battery,
            "battery_resume_at_percent": max(
                minimum_battery, battery_resume_percent or minimum_battery
            ),
            "battery_heavy_task_cooldown_seconds": heavy_task_cooldown_seconds,
            "power_status_updated_at": (
                self.updated_at.isoformat() if self.updated_at is not None else None
            ),
        }


def _ssl_context(ca_file: Path | None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)


def _read_token(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"worker credential file is empty: {path}")
    return value


def _write_private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value.rstrip() + "\n")
    os.replace(temporary, path)


def _read_optional_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def _can_recover_worker_credential(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except json.JSONDecodeError:
        return False
    if not isinstance(body, dict):
        return False
    detail = body.get("detail")
    return isinstance(detail, str) and detail in {
        "worker capabilities exceed its enrollment policy",
        "worker identity does not match credential",
    }


def _worker_protocol_version() -> int:
    # This is executable compatibility metadata, not an operator preference.
    # Derive it from the installed agent code so a stale preserved .env cannot
    # make an old worker impersonate a newer contract (or vice versa).
    return WORKER_PROTOCOL_VERSION


def _identity(
    hostname: str,
    capabilities: dict[str, bool],
    *,
    machine_group: str = "",
    accepted_queues: list[str] | None = None,
    maximum_concurrency: int = 1,
    active_jobs: int = 0,
    available_models: list[str] | None = None,
    model_runtime_routes: dict[str, str] | None = None,
    storage_path: Path | None = None,
    runtime_details: dict[str, object] | None = None,
) -> dict[str, Any]:
    storage_root = storage_path or Path(tempfile.gettempdir())
    load = _runtime_load(active_jobs, maximum_concurrency)
    return {
        "hostname_label": hostname,
        "machine_group": machine_group,
        "architecture": platform.machine(),
        "operating_system": platform.platform(),
        "software_version": __version__,
        "release_version": os.environ.get("STAR_WORKER_RELEASE_VERSION", "unversioned"),
        "build_git_sha": os.environ.get("STAR_BUILD_GIT_SHA", "unknown"),
        "build_tree_state": os.environ.get("STAR_BUILD_TREE_STATE", "unknown"),
        "build_source_fingerprint": os.environ.get("STAR_BUILD_SOURCE_FINGERPRINT", "unknown"),
        "worker_protocol_version": _worker_protocol_version(),
        "worker_contract_hash": WORKER_CONTRACT_HASH,
        "task_contract_revisions": TASK_EXECUTOR_REVISIONS,
        "capabilities": capabilities,
        "current_load": load,
        "free_storage_bytes": shutil.disk_usage(storage_root).free,
        "temperature_c": load.get("gpu_temperature_c"),
        "power_state": "unknown",
        "network_status": "online",
        "available_models": available_models or [],
        "model_runtime_routes": model_runtime_routes or {},
        "accepted_queues": accepted_queues or [],
        "maximum_concurrency": maximum_concurrency,
        **(runtime_details or {}),
    }


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _device_private_key(path: Path) -> Ed25519PrivateKey:
    encoded = _read_optional_file(path)
    if encoded:
        try:
            private_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            return Ed25519PrivateKey.from_private_bytes(private_bytes)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"worker device identity is invalid: {path}") from exc
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _write_private_file(path, _encode_urlsafe(private_bytes))
    return private_key


async def _automatically_enroll(
    client: httpx.AsyncClient,
    *,
    token_path: Path,
    identity_path: Path,
    heartbeat: dict[str, Any],
) -> str:
    # Sign the same normalized document that the coordinator verifies after
    # Pydantic validation. Signing the raw dictionary allowed ignored
    # runtime-only fields or normalized numbers to cause 401 recovery loops.
    normalized_heartbeat = WorkerHeartbeat.model_validate(heartbeat).model_dump(
        mode="json",
        exclude_unset=True,
    )
    private_key = _device_private_key(identity_path)
    public_key = _encode_urlsafe(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    issued_at = datetime.now(UTC)
    nonce = secrets.token_urlsafe(32)

    def signed_request(heartbeat_value: dict[str, Any]) -> dict[str, Any]:
        message = automatic_enrollment_message(
            heartbeat=heartbeat_value,
            device_public_key=public_key,
            issued_at=issued_at,
            nonce=nonce,
        )
        return {
            "heartbeat": heartbeat_value,
            "device_public_key": public_key,
            "issued_at": issued_at.isoformat(),
            "nonce": nonce,
            "signature": _encode_urlsafe(private_key.sign(message)),
        }

    response = await client.post(
        "/api/v1/workers/automatic-enroll",
        json=signed_request(normalized_heartbeat),
    )
    if response.status_code == 401 and "worker_protocol_version" in normalized_heartbeat:
        legacy_heartbeat = {
            key: value
            for key, value in normalized_heartbeat.items()
            if key != "worker_protocol_version"
        }
        response = await client.post(
            "/api/v1/workers/automatic-enroll",
            json=signed_request(legacy_heartbeat),
        )
    response.raise_for_status()
    payload = response.json()
    token = str(payload["worker_token"])
    _write_private_file(token_path, token)
    _write_private_file(
        Path(str(token_path) + ".expires"),
        str(payload["credential_expires_at"]),
    )
    client.headers["Authorization"] = "Bearer " + token
    logger.info(
        "worker authenticated automatically",
        extra={"hostname": normalized_heartbeat["hostname_label"]},
    )
    return token


async def _power_policy_allows_work(
    *,
    require_ac_power: bool,
    minimum_battery: int,
    battery_resume_percent: int | None = None,
    power_status: PowerAdmissionState | None = None,
) -> bool:
    if power_status is not None:
        return power_status.allows_work(
            require_ac_power=require_ac_power,
            minimum_battery=minimum_battery,
            battery_resume_percent=battery_resume_percent,
        )
    if platform.system() != "Darwin" or (not require_ac_power and minimum_battery <= 0):
        return True
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/pmset",
        "-g",
        "batt",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return False
    status_text = stdout.decode("utf-8", errors="replace")
    on_ac_power = "AC Power" in status_text
    battery = 0
    for token in status_text.replace(";", " ").split():
        if token.endswith("%") and token[:-1].isdigit():
            battery = int(token[:-1])
            break
    return (not require_ac_power or on_ac_power) and battery >= minimum_battery


async def _submit_failure(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    error_class: str,
    *,
    retryable: bool,
    reason: str | None = None,
    source_asset_id: str | None = None,
    source_error_code: str | None = None,
) -> None:
    # A dropped failure report leaves the task to slow lease-TTL recovery, so
    # transient submission errors get two quick retries before giving up.
    last_error: Exception | None = None
    for delay in (0.0, 1.0, 4.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            response = await client.post(
                "/api/v1/workers/tasks/" + str(task["task_id"]) + "/fail",
                params={"lease_id": task["lease_id"]},
                timeout=10.0,
                json={
                    "error_class": error_class,
                    "retryable": retryable,
                    **({"reason": reason} if reason is not None else {}),
                    **({"source_asset_id": source_asset_id} if source_asset_id is not None else {}),
                    **(
                        {"source_error_code": source_error_code}
                        if source_error_code is not None
                        else {}
                    ),
                },
            )
            response.raise_for_status()
            return
        except httpx.RequestError as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_error = exc
    if last_error is not None:
        raise last_error


async def _renew_lease(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    *,
    stage: str,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"progress": {"stage": stage}}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    response = await client.post(
        "/api/v1/workers/tasks/" + str(task["task_id"]) + "/heartbeat",
        params={"lease_id": task["lease_id"]},
        json=payload,
        timeout=10.0,
    )
    if response.status_code in {204, 404, 409}:
        raise LeaseLostError("task lease was superseded")
    response.raise_for_status()
    if response.json().get("cancel_requested") is True:
        raise TaskCancelledError("task cancellation was requested")


async def _maintain_lease(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    finished: asyncio.Event,
) -> None:
    consecutive_failures = 0
    while not finished.is_set():
        try:
            await asyncio.wait_for(finished.wait(), timeout=TASK_HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            pass
        try:
            await _renew_lease(client, task, stage="processing")
            consecutive_failures = 0
        except LeaseLostError, TaskCancelledError:
            raise
        except httpx.HTTPError as exc:
            consecutive_failures += 1
            if consecutive_failures >= TASK_HEARTBEAT_FAILURE_LIMIT:
                raise LeaseLostError("control plane could not renew the task lease") from exc


async def _rotate_credential_if_needed(
    client: httpx.AsyncClient,
    token_path: Path,
) -> None:
    expiry_path = Path(str(token_path) + ".expires")
    expires_at: datetime | None = None
    expiry_text = await asyncio.to_thread(_read_optional_file, expiry_path)
    if expiry_text:
        with contextlib.suppress(ValueError):
            expires_at = datetime.fromisoformat(expiry_text)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and expires_at > datetime.now(UTC) + timedelta(days=7):
        return
    response = await client.post("/api/v1/workers/credential/rotate")
    response.raise_for_status()
    payload = response.json()
    token = str(payload["worker_token"])
    _write_private_file(token_path, token)
    _write_private_file(expiry_path, str(payload["credential_expires_at"]))
    client.headers["Authorization"] = "Bearer " + token
