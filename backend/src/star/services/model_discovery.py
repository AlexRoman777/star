from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import ModelVersion, Worker, WorkerModelAvailability
from star.domain.schemas import WorkerModelInventoryItem
from star.services.audit import AuditRecord, append_audit_event
from star.services.operations import OperationRecord, append_operation_event


def _runtime_fingerprint(item: WorkerModelInventoryItem) -> str:
    evidence = {
        "architecture": item.architecture,
        "max_context_length": item.max_context_length,
        "model_key": item.model_key,
        "quantization": item.quantization,
        "runtime": item.runtime,
        "runtime_version": item.runtime_version,
        "runtime_format": item.runtime_format,
        "size_bytes": item.size_bytes,
        "version": item.version,
        "vision": item.vision,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _approved_model_capability(worker: Worker) -> str | None:
    """Choose capacity only from capabilities advertised *and* allowed by enrollment."""

    advertised = {
        name
        for name, enabled in worker.capabilities.items()
        if isinstance(name, str) and enabled is True
    }
    approved = advertised.intersection(worker.allowed_capabilities)
    if "mlx" in approved:
        return "mlx"
    if "large-vlm" in approved:
        return "large-vlm"
    if approved.intersection({"medium-vlm", "multi-image-review"}):
        return "medium-vlm"
    if "small-vlm" in approved:
        return "small-vlm"
    return None


def _resource_profile(
    worker: Worker,
    item: WorkerModelInventoryItem,
    *,
    capability: str,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "discovery": "worker-runtime",
        "worker_id": str(worker.id),
        "worker_hostname": worker.hostname_label,
        "runtime_format": item.runtime_format,
        "runtime_version": item.runtime_version,
        "seed_supported": item.seed_supported,
        "runtime_fingerprint": _runtime_fingerprint(item),
        "architecture": item.architecture,
        "size_bytes": item.size_bytes,
        "max_context_length": item.max_context_length,
        "loaded_when_discovered": item.loaded,
        "capability": capability,
    }
    if item.artifact_checksum is not None:
        profile["reported_artifact_checksum"] = item.artifact_checksum
    return profile


def _canonical_resource_profile(
    item: WorkerModelInventoryItem, *, capability: str
) -> dict[str, object]:
    """Stable identity metadata that cannot be overwritten by another machine."""

    return {
        "discovery": "worker-runtime",
        "identity_scope": "canonical-model",
        "runtime_format": item.runtime_format,
        "vision": item.vision,
        "reasoning": item.reasoning,
        "capability": capability,
    }


async def synchronize_discovered_models(
    session: AsyncSession,
    *,
    worker: Worker,
    inventory: list[WorkerModelInventoryItem],
    correlation_id: str,
    automatically_ready_models: set[str] | None = None,
) -> tuple[int, int]:
    """Register bounded runtime inventory and ready only explicitly configured models."""

    registered = 0
    existing_count = 0
    advertised = set(worker.runtime_status.get("available_models", []))
    ready_models = automatically_ready_models or set()
    capability = _approved_model_capability(worker)
    if capability is None:
        return registered, existing_count
    known_availability = {
        value.model_version_id: value
        for value in (
            await session.scalars(
                select(WorkerModelAvailability).where(
                    WorkerModelAvailability.worker_id == worker.id
                )
            )
        ).all()
    }
    for value in known_availability.values():
        value.available = False
    for item in inventory:
        if item.model_key not in advertised:
            continue
        model = await session.scalar(
            select(ModelVersion).where(
                ModelVersion.model_id == item.model_key,
                ModelVersion.version == item.version,
                ModelVersion.runtime == item.runtime,
                ModelVersion.quantization == item.quantization,
            )
        )
        automatically_ready = bool(
            item.model_key in ready_models and item.vision and item.artifact_checksum
        )
        created = False
        if model is None:
            candidate = ModelVersion(
                model_id=item.model_key,
                version=item.version,
                runtime=item.runtime,
                quantization=item.quantization,
                license_id=None,
                artifact_checksum=item.artifact_checksum if automatically_ready else None,
                capabilities={
                    "images": item.vision,
                    "structured_json": item.vision,
                    "single_image_review": item.vision,
                    "multi_image": item.vision,
                    "reasoning": item.reasoning,
                },
                # This canonical row is immutable shared identity. Per-machine
                # architecture, loading, and fingerprints live below.
                resource_profile=_canonical_resource_profile(item, capability=capability),
                benchmark=(
                    {
                        "mode": "trusted_local_runtime",
                        "status": "ready",
                        "benchmark_optional": True,
                        "proposal_only": True,
                    }
                    if automatically_ready
                    else {}
                ),
                enabled=automatically_ready,
            )
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
                model = candidate
                created = True
            except IntegrityError:
                model = await session.scalar(
                    select(ModelVersion).where(
                        ModelVersion.model_id == item.model_key,
                        ModelVersion.version == item.version,
                        ModelVersion.runtime == item.runtime,
                        ModelVersion.quantization == item.quantization,
                    )
                )
        if model is None:
            raise RuntimeError("canonical model registration did not settle")
        if automatically_ready and not model.enabled:
            model.enabled = True
            if model.artifact_checksum is None:
                model.artifact_checksum = item.artifact_checksum
            model.benchmark = {
                "mode": "trusted_local_runtime",
                "status": "ready",
                "benchmark_optional": True,
                "proposal_only": True,
            }

        availability = known_availability.get(model.id)
        evidence = _resource_profile(worker, item, capability=capability)
        if availability is None:
            availability = WorkerModelAvailability(
                worker_id=worker.id,
                model_version_id=model.id,
                runtime_fingerprint=_runtime_fingerprint(item),
                capability=capability,
                runtime_evidence=evidence,
            )
            session.add(availability)
            known_availability[model.id] = availability
        availability.available = True
        availability.loaded = item.loaded
        availability.artifact_checksum = item.artifact_checksum
        availability.runtime_fingerprint = _runtime_fingerprint(item)
        availability.capability = capability
        availability.runtime_evidence = evidence
        availability.last_seen_at = utc_now()

        if created:
            await append_audit_event(
                session,
                AuditRecord(
                    event_type="model.discovered",
                    actor_type="worker",
                    actor_id=str(worker.id),
                    target_ref={"model_version_id": str(model.id)},
                    correlation_id=correlation_id,
                    after={
                        "model_id": model.model_id,
                        "version": model.version,
                        "runtime": model.runtime,
                        "enabled": automatically_ready,
                    },
                ),
            )
            await append_operation_event(
                session,
                OperationRecord(
                    source="model",
                    event_type="model.discovered",
                    message="A worker discovered a local model candidate",
                    correlation_id=correlation_id,
                    worker_id=worker.id,
                    model_id=model.id,
                    details={
                        "model_key": model.model_id,
                        "runtime": model.runtime,
                        "state": "ready" if automatically_ready else "disabled",
                    },
                ),
            )
            registered += 1
        else:
            existing_count += 1
    await session.flush()
    return registered, existing_count
