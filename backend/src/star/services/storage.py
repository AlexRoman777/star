from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.models import (
    LibraryProgram,
    ModelPackage,
    NormalizedDerivative,
    StorageInventory,
)
from star.domain.enums import LibraryProgramState
from star.domain.schemas import StorageStatusResponse
from star.services.operations import OperationRecord, append_operation_event
from star.services.storage_inventory import INVENTORY_SCOPE

STORAGE_STATUS_TTL_SECONDS = 30.0
_storage_status_cache: dict[
    tuple[int, str, str, int, int, int], tuple[float, StorageStatusResponse]
] = {}


def _existing_path(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


async def _database_size(session: AsyncSession) -> int:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return 0
    value = await session.scalar(text("select pg_database_size(current_database())"))
    return int(value or 0)


def _available_bytes(path: Path) -> int:
    stats = os.statvfs(_existing_path(path))
    return int(stats.f_bavail) * int(stats.f_frsize or stats.f_bsize)


async def get_storage_status(
    session: AsyncSession,
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> StorageStatusResponse:
    cache_key = (
        id(session.get_bind()),
        str(settings.model_root.resolve()),
        str(settings.artifact_root.resolve()),
        settings.artifact_cache_max_bytes,
        settings.artifact_cache_target_bytes,
        settings.managed_storage_hard_limit_bytes,
    )
    now = time.monotonic()
    cached = _storage_status_cache.get(cache_key)
    if not force_refresh and cached is not None and now - cached[0] < STORAGE_STATUS_TTL_SECONDS:
        return cached[1]
    inventory = await session.get(StorageInventory, INVENTORY_SCOPE)
    registered_model_package_bytes = int(
        await session.scalar(select(func.coalesce(func.sum(ModelPackage.size_bytes), 0))) or 0
    )
    has_inventory = inventory is not None and inventory.is_complete
    tracked_artifact_bytes = int(
        await session.scalar(select(func.coalesce(func.sum(NormalizedDerivative.size_bytes), 0)))
        or 0
    )
    artifact_bytes = max(
        tracked_artifact_bytes,
        int(inventory.artifact_allocated_bytes) if has_inventory and inventory else 0,
    )
    model_bytes = (
        int(inventory.coordinator_model_allocated_bytes) if has_inventory and inventory else 0
    )
    database_bytes = await _database_size(session)
    managed_bytes = artifact_bytes + model_bytes + database_bytes
    hard_limit = settings.managed_storage_hard_limit_bytes
    warning_threshold = int(hard_limit * settings.managed_storage_warning_ratio)
    volume = await asyncio.to_thread(shutil.disk_usage, _existing_path(settings.model_root))
    volume_available = await asyncio.to_thread(_available_bytes, settings.model_root)
    filesystem_reserve = min(settings.storage_min_free_bytes, max(0, volume.total // 2))
    if managed_bytes >= hard_limit:
        storage_state = "limit_reached"
    elif volume_available <= filesystem_reserve:
        storage_state = "low_space"
    elif managed_bytes >= warning_threshold:
        storage_state = "warning"
    else:
        storage_state = "healthy"
    artifact_cache_max = int(settings.artifact_cache_max_bytes)
    artifact_cache_target = min(int(settings.artifact_cache_target_bytes), artifact_cache_max)
    if artifact_cache_max <= 0:
        artifact_cache_status = "disabled"
    elif artifact_bytes >= artifact_cache_max:
        artifact_cache_status = "limit_reached"
    elif artifact_bytes >= artifact_cache_target:
        artifact_cache_status = "cleanup_due"
    else:
        artifact_cache_status = "healthy"
    response = StorageStatusResponse(
        artifact_bytes=artifact_bytes,
        artifact_tracked_bytes=tracked_artifact_bytes,
        artifact_filesystem_logical_bytes=(
            int(inventory.artifact_logical_bytes) if has_inventory and inventory else None
        ),
        artifact_filesystem_allocated_bytes=(
            int(inventory.artifact_allocated_bytes) if has_inventory and inventory else None
        ),
        artifact_file_count=(
            int(inventory.artifact_file_count) if has_inventory and inventory else 0
        ),
        artifact_mapping_count=int(
            await session.scalar(select(func.count(NormalizedDerivative.id))) or 0
        ),
        preview_file_count=0,
        preview_mapping_count=0,
        fullsize_file_count=0,
        fullsize_mapping_count=0,
        artifact_cache_max_bytes=artifact_cache_max,
        artifact_cache_target_bytes=artifact_cache_target,
        artifact_cache_utilization=(
            artifact_bytes / artifact_cache_max if artifact_cache_max else 0
        ),
        artifact_cache_status=artifact_cache_status,
        model_bytes=model_bytes,
        registered_model_package_bytes=registered_model_package_bytes,
        coordinator_model_logical_bytes=(
            int(inventory.coordinator_model_logical_bytes) if inventory else 0
        ),
        coordinator_model_allocated_bytes=model_bytes,
        coordinator_model_file_count=(
            int(inventory.coordinator_model_file_count) if inventory else 0
        ),
        coordinator_model_missing_count=(
            int(inventory.coordinator_model_missing_count) if inventory else 0
        ),
        database_bytes=database_bytes,
        managed_bytes=managed_bytes,
        hard_limit_bytes=hard_limit,
        warning_threshold_bytes=warning_threshold,
        volume_used_bytes=volume.used,
        volume_free_bytes=volume.free,
        volume_available_bytes=volume_available,
        volume_total_bytes=volume.total,
        filesystem_reserve_bytes=filesystem_reserve,
        inventory_scanned_at=inventory.scanned_at if inventory else None,
        inventory_complete=has_inventory,
        inventory_scan_duration_ms=(int(inventory.scan_duration_ms) if inventory else None),
        mapped_artifact_bytes=(int(inventory.mapped_allocated_bytes) if inventory else None),
        mapped_artifact_file_count=(int(inventory.mapped_file_count) if inventory else 0),
        orphan_artifact_bytes=(int(inventory.orphan_allocated_bytes) if inventory else 0),
        orphan_artifact_file_count=(int(inventory.orphan_file_count) if inventory else 0),
        partial_artifact_bytes=(int(inventory.partial_allocated_bytes) if inventory else 0),
        partial_artifact_file_count=(int(inventory.partial_file_count) if inventory else 0),
        unknown_artifact_bytes=(int(inventory.unknown_allocated_bytes) if inventory else 0),
        unknown_artifact_file_count=(int(inventory.unknown_file_count) if inventory else 0),
        missing_artifact_bytes=(int(inventory.missing_mapping_bytes) if inventory else 0),
        missing_artifact_file_count=(int(inventory.missing_mapping_count) if inventory else 0),
        mismatched_artifact_file_count=(int(inventory.mismatched_file_count) if inventory else 0),
        artifact_symlink_count=(int(inventory.symlink_count) if inventory else 0),
        inventory_error_count=(int(inventory.scan_error_count) if inventory else 0),
        cleanup_deleted_bytes=(int(inventory.cleanup_deleted_bytes) if inventory else 0),
        cleanup_deleted_files=(int(inventory.cleanup_deleted_files) if inventory else 0),
        utilization=managed_bytes / hard_limit if hard_limit else 0,
        status=storage_state,
    )
    if len(_storage_status_cache) >= 64:
        _storage_status_cache.clear()
    _storage_status_cache[cache_key] = (now, response)
    return response


async def pause_library_program_at_storage_limit(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    status: StorageStatusResponse,
) -> bool:
    """Pause new continuous work once Star reaches its managed-storage cap."""

    if status.status not in {"limit_reached", "low_space"}:
        return False
    if program.state != LibraryProgramState.RUNNING:
        return True
    # Existing leases may finish and submit their bounded results. Keeping the
    # program enabled but paused makes recovery an explicit human action.
    program.state = LibraryProgramState.PAUSED
    program.revision += 1
    program.last_error_class = (
        "CoordinatorFilesystemReserveReached"
        if status.status == "low_space"
        else "ManagedStorageLimitReached"
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="storage.limit.paused",
            message="Continuous library analysis paused at the managed storage limit",
            severity="critical",
            details={
                "state": program.state.value,
                "managed_bytes": status.managed_bytes,
                "hard_limit_bytes": status.hard_limit_bytes,
            },
        ),
    )
    return True
