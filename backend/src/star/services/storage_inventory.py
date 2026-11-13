from __future__ import annotations

import asyncio
import errno
import os
import re
import stat
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import case, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import (
    MediaStagingSuppression,
    ModelPackage,
    NormalizedDerivative,
    OperationalEvent,
    StorageInventory,
    Task,
)
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_contract import ACTIVE_TASK_STATES

INVENTORY_SCOPE = "coordinator"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PARTIAL = re.compile(r"^(?P<digest>[0-9a-f]{64})\.partial-[0-9a-f]+$")
_ADMISSION_EVENT_COOLDOWN = timedelta(minutes=5)


class StorageAdmissionError(OSError):
    """A bounded cache write was rejected before touching the filesystem."""

    def __init__(self, reason: str) -> None:
        super().__init__(errno.ENOSPC, reason)
        self.reason = reason


@dataclass(frozen=True)
class _CleanupCandidate:
    relative_path: str
    logical_bytes: int
    allocated_bytes: int
    device: int
    inode: int
    category: str
    digest: str | None = None


@dataclass(frozen=True)
class ArtifactFilesystemScan:
    is_complete: bool
    logical_bytes: int
    allocated_bytes: int
    file_count: int
    mapped_logical_bytes: int
    mapped_allocated_bytes: int
    mapped_file_count: int
    orphan_logical_bytes: int
    orphan_allocated_bytes: int
    orphan_file_count: int
    partial_logical_bytes: int
    partial_allocated_bytes: int
    partial_file_count: int
    unknown_logical_bytes: int
    unknown_allocated_bytes: int
    unknown_file_count: int
    missing_mapping_bytes: int
    missing_mapping_count: int
    mismatched_file_count: int
    symlink_count: int
    error_count: int
    cleanup_candidates: tuple[_CleanupCandidate, ...]


@dataclass(frozen=True)
class ModelFilesystemScan:
    registered_package_bytes: int
    logical_bytes: int
    allocated_bytes: int
    file_count: int
    missing_count: int


@dataclass(frozen=True)
class StorageAdmission:
    allowed: bool
    reason: str | None
    incoming_bytes: int
    additional_bytes: int
    artifact_accounted_bytes: int
    database_bytes: int
    coordinator_model_bytes: int
    projected_managed_bytes: int
    managed_limit_bytes: int
    volume_available_bytes: int
    filesystem_reserve_bytes: int


def _allocated_bytes(file_stat: os.stat_result) -> int:
    blocks = getattr(file_stat, "st_blocks", None)
    return int(blocks) * 512 if blocks is not None else int(file_stat.st_size)


def _safe_stat(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except FileNotFoundError, PermissionError, OSError:
        return None


def _empty_artifact_scan(
    expected: dict[str, tuple[str, int]],
    *,
    complete: bool,
    errors: int = 0,
    symlinks: int = 0,
) -> ArtifactFilesystemScan:
    return ArtifactFilesystemScan(
        is_complete=complete,
        logical_bytes=0,
        allocated_bytes=0,
        file_count=0,
        mapped_logical_bytes=0,
        mapped_allocated_bytes=0,
        mapped_file_count=0,
        orphan_logical_bytes=0,
        orphan_allocated_bytes=0,
        orphan_file_count=0,
        partial_logical_bytes=0,
        partial_allocated_bytes=0,
        partial_file_count=0,
        unknown_logical_bytes=0,
        unknown_allocated_bytes=0,
        unknown_file_count=0,
        missing_mapping_bytes=sum(size for _path, size in expected.values()),
        missing_mapping_count=len(expected),
        mismatched_file_count=0,
        symlink_count=symlinks,
        error_count=errors,
        cleanup_candidates=(),
    )


def scan_artifact_filesystem(
    root: Path,
    expected: dict[str, tuple[str, int]],
    *,
    now: datetime,
    orphan_grace: timedelta,
    partial_grace: timedelta,
    cleanup_candidate_limit: int,
) -> ArtifactFilesystemScan:
    """Scan only the fixed content-addressed layout without following links."""

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return _empty_artifact_scan(expected, complete=True)
    except OSError:
        return _empty_artifact_scan(expected, complete=False, errors=1)
    if stat.S_ISLNK(root_stat.st_mode):
        return _empty_artifact_scan(expected, complete=False, errors=1, symlinks=1)

    object_root = root / "sha256"
    try:
        object_root_stat = object_root.lstat()
    except FileNotFoundError:
        return _empty_artifact_scan(expected, complete=True)
    except OSError:
        return _empty_artifact_scan(expected, complete=False, errors=1)
    if stat.S_ISLNK(object_root_stat.st_mode):
        return _empty_artifact_scan(expected, complete=False, errors=1, symlinks=1)
    if not stat.S_ISDIR(object_root_stat.st_mode):
        return _empty_artifact_scan(expected, complete=False, errors=1)

    counters = {
        "logical": 0,
        "allocated": 0,
        "files": 0,
        "mapped_logical": 0,
        "mapped_allocated": 0,
        "mapped_files": 0,
        "orphan_logical": 0,
        "orphan_allocated": 0,
        "orphan_files": 0,
        "partial_logical": 0,
        "partial_allocated": 0,
        "partial_files": 0,
        "unknown_logical": 0,
        "unknown_allocated": 0,
        "unknown_files": 0,
        "mismatched": 0,
        "symlinks": 0,
        "errors": 0,
    }
    seen_mapped: set[str] = set()
    candidates: list[_CleanupCandidate] = []
    now_timestamp = now.timestamp()

    try:
        prefix_entries = list(os.scandir(object_root))
    except OSError:
        return _empty_artifact_scan(expected, complete=False, errors=1)
    for prefix_entry in prefix_entries:
        prefix_stat = _safe_stat(prefix_entry)
        if prefix_stat is None:
            counters["errors"] += 1
            continue
        if stat.S_ISLNK(prefix_stat.st_mode):
            counters["symlinks"] += 1
            continue
        if not stat.S_ISDIR(prefix_stat.st_mode):
            counters["errors"] += 1
            continue
        if re.fullmatch(r"[0-9a-f]{2}", prefix_entry.name) is None:
            # Unknown directories are never traversed or automatically removed.
            counters["errors"] += 1
            continue
        try:
            file_entries = list(os.scandir(prefix_entry.path))
        except OSError:
            counters["errors"] += 1
            continue
        for file_entry in file_entries:
            file_stat = _safe_stat(file_entry)
            if file_stat is None:
                counters["errors"] += 1
                continue
            if stat.S_ISLNK(file_stat.st_mode):
                counters["symlinks"] += 1
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                counters["errors"] += 1
                continue

            logical = int(file_stat.st_size)
            allocated = _allocated_bytes(file_stat)
            relative = str(Path("sha256") / prefix_entry.name / file_entry.name)
            counters["logical"] += logical
            counters["allocated"] += allocated
            counters["files"] += 1
            partial_match = _PARTIAL.fullmatch(file_entry.name)
            if partial_match is not None:
                counters["partial_logical"] += logical
                counters["partial_allocated"] += allocated
                counters["partial_files"] += 1
                is_old = file_stat.st_mtime <= now_timestamp - partial_grace.total_seconds()
                if is_old and len(candidates) < cleanup_candidate_limit:
                    candidates.append(
                        _CleanupCandidate(
                            relative_path=relative,
                            logical_bytes=logical,
                            allocated_bytes=allocated,
                            device=int(file_stat.st_dev),
                            inode=int(file_stat.st_ino),
                            category="partial",
                        )
                    )
                continue

            digest = file_entry.name
            is_content_addressed = (
                _DIGEST.fullmatch(digest) is not None and digest[:2] == prefix_entry.name
            )
            expected_record = expected.get(digest) if is_content_addressed else None
            if expected_record is not None and expected_record[0] == relative:
                seen_mapped.add(digest)
                counters["mapped_logical"] += logical
                counters["mapped_allocated"] += allocated
                counters["mapped_files"] += 1
                if expected_record[1] != logical:
                    counters["mismatched"] += 1
                continue
            if is_content_addressed:
                counters["orphan_logical"] += logical
                counters["orphan_allocated"] += allocated
                counters["orphan_files"] += 1
                is_old = file_stat.st_mtime <= now_timestamp - orphan_grace.total_seconds()
                if is_old and len(candidates) < cleanup_candidate_limit:
                    candidates.append(
                        _CleanupCandidate(
                            relative_path=relative,
                            logical_bytes=logical,
                            allocated_bytes=allocated,
                            device=int(file_stat.st_dev),
                            inode=int(file_stat.st_ino),
                            category="orphan",
                            digest=digest,
                        )
                    )
                continue
            counters["unknown_logical"] += logical
            counters["unknown_allocated"] += allocated
            counters["unknown_files"] += 1

    missing = set(expected) - seen_mapped
    return ArtifactFilesystemScan(
        is_complete=counters["errors"] == 0,
        logical_bytes=counters["logical"],
        allocated_bytes=counters["allocated"],
        file_count=counters["files"],
        mapped_logical_bytes=counters["mapped_logical"],
        mapped_allocated_bytes=counters["mapped_allocated"],
        mapped_file_count=counters["mapped_files"],
        orphan_logical_bytes=counters["orphan_logical"],
        orphan_allocated_bytes=counters["orphan_allocated"],
        orphan_file_count=counters["orphan_files"],
        partial_logical_bytes=counters["partial_logical"],
        partial_allocated_bytes=counters["partial_allocated"],
        partial_file_count=counters["partial_files"],
        unknown_logical_bytes=counters["unknown_logical"],
        unknown_allocated_bytes=counters["unknown_allocated"],
        unknown_file_count=counters["unknown_files"],
        missing_mapping_bytes=sum(expected[digest][1] for digest in missing),
        missing_mapping_count=len(missing),
        mismatched_file_count=counters["mismatched"],
        symlink_count=counters["symlinks"],
        error_count=counters["errors"],
        cleanup_candidates=tuple(candidates),
    )


def _safe_model_relative_path(relative_path: str) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return None
    return candidate


def scan_coordinator_models(
    root: Path,
    packages: list[tuple[str | None, int]],
) -> ModelFilesystemScan:
    registered_bytes = sum(max(0, int(size)) for _path, size in packages)
    logical_bytes = 0
    allocated_bytes = 0
    file_count = 0
    missing_count = 0
    seen_paths: set[str] = set()
    for relative_path, _declared_size in packages:
        if relative_path is None or relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        relative = _safe_model_relative_path(relative_path)
        if relative is None:
            missing_count += 1
            continue
        current = root
        is_unsafe = False
        for part in relative.parts[:-1]:
            current = current / part
            try:
                parent_stat = current.lstat()
            except FileNotFoundError, OSError:
                is_unsafe = True
                break
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                is_unsafe = True
                break
        if is_unsafe:
            missing_count += 1
            continue
        path = root / relative
        try:
            file_stat = path.lstat()
        except FileNotFoundError, OSError:
            missing_count += 1
            continue
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            missing_count += 1
            continue
        logical_bytes += int(file_stat.st_size)
        allocated_bytes += _allocated_bytes(file_stat)
        file_count += 1
    return ModelFilesystemScan(
        registered_package_bytes=registered_bytes,
        logical_bytes=logical_bytes,
        allocated_bytes=allocated_bytes,
        file_count=file_count,
        missing_count=missing_count,
    )


def _unlink_candidate(root: Path, candidate: _CleanupCandidate) -> bool:
    path = root / candidate.relative_path
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode):
        return False
    if int(current.st_dev) != candidate.device or int(current.st_ino) != candidate.inode:
        return False
    try:
        path.unlink()
    except FileNotFoundError, PermissionError, OSError:
        return False
    return True


async def _cleanup_scan_candidates(
    session: AsyncSession,
    *,
    root: Path,
    scan: ArtifactFilesystemScan,
    max_files: int,
) -> tuple[ArtifactFilesystemScan, int, int]:
    if max_files <= 0 or not scan.cleanup_candidates:
        return scan, 0, 0
    live_digests: set[str] = set()
    deleted: list[_CleanupCandidate] = []
    for candidate in scan.cleanup_candidates:
        if len(deleted) >= max_files:
            break
        if candidate.digest is not None and candidate.digest in live_digests:
            continue
        if await asyncio.to_thread(_unlink_candidate, root, candidate):
            deleted.append(candidate)
    if not deleted:
        return scan, 0, 0

    deleted_logical = sum(item.logical_bytes for item in deleted)
    deleted_allocated = sum(item.allocated_bytes for item in deleted)
    orphan_deleted = [item for item in deleted if item.category == "orphan"]
    partial_deleted = [item for item in deleted if item.category == "partial"]
    adjusted = replace(
        scan,
        logical_bytes=max(0, scan.logical_bytes - deleted_logical),
        allocated_bytes=max(0, scan.allocated_bytes - deleted_allocated),
        file_count=max(0, scan.file_count - len(deleted)),
        orphan_logical_bytes=max(
            0, scan.orphan_logical_bytes - sum(item.logical_bytes for item in orphan_deleted)
        ),
        orphan_allocated_bytes=max(
            0,
            scan.orphan_allocated_bytes - sum(item.allocated_bytes for item in orphan_deleted),
        ),
        orphan_file_count=max(0, scan.orphan_file_count - len(orphan_deleted)),
        partial_logical_bytes=max(
            0, scan.partial_logical_bytes - sum(item.logical_bytes for item in partial_deleted)
        ),
        partial_allocated_bytes=max(
            0,
            scan.partial_allocated_bytes - sum(item.allocated_bytes for item in partial_deleted),
        ),
        partial_file_count=max(0, scan.partial_file_count - len(partial_deleted)),
        cleanup_candidates=(),
    )
    return adjusted, len(deleted), deleted_allocated


def _unlink_evicted(root: Path, digest: str, relative: str) -> bool:
    if _DIGEST.fullmatch(digest) is None or relative != f"sha256/{digest[:2]}/{digest}":
        return False
    path = root / relative
    try:
        current = path.lstat()
    except FileNotFoundError, OSError:
        return False
    if not stat.S_ISREG(current.st_mode):
        return False
    try:
        path.unlink()
    except FileNotFoundError, PermissionError, OSError:
        return False
    return True


async def _evict_cold_derivatives(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime,
) -> tuple[int, int]:
    """Shrink the mapped derivative cache toward its target with tiered LRU.

    Model-input previews are evicted before gallery thumbnails, oldest access
    first. Rows are deleted and committed before their files are unlinked so a
    crash leaves only an orphaned file for the grace-based cleanup to reap,
    never a mapped row without its content.
    """

    target = int(settings.artifact_cache_target_bytes)
    tracked = await _tracked_artifact_bytes(session)
    if tracked <= target:
        return 0, 0
    grace_cutoff = now - timedelta(days=settings.staging_eviction_grace_days)
    variant_priority = case((NormalizedDerivative.variant == "preview", 0), else_=1)
    candidates = list(
        await session.scalars(
            select(NormalizedDerivative)
            .where(
                NormalizedDerivative.last_accessed_at < grace_cutoff,
                ~exists(
                    select(Task.id).where(
                        Task.asset_id == NormalizedDerivative.asset_id,
                        Task.asset_revision_id == NormalizedDerivative.asset_revision_id,
                        Task.task_type.in_({"asset.primary_review", "asset.deep_review"}),
                        Task.state.in_(ACTIVE_TASK_STATES),
                    )
                ),
            )
            .order_by(variant_priority, NormalizedDerivative.last_accessed_at.asc())
            .limit(max(0, int(settings.storage_cleanup_max_files)))
        )
    )
    if not candidates:
        return 0, 0
    to_reclaim = tracked - target
    evicted: list[tuple[str, str]] = []
    evicted_bytes = 0
    for row in candidates:
        if evicted_bytes >= to_reclaim:
            break
        digest = str(row.content_digest)
        relative = str(row.relative_path)
        size = int(row.size_bytes)
        suppression = await session.get(MediaStagingSuppression, row.asset_id)
        if suppression is None:
            session.add(
                MediaStagingSuppression(
                    asset_id=row.asset_id,
                    asset_revision_id=row.asset_revision_id,
                    reason="budget_eviction",
                )
            )
        else:
            suppression.asset_revision_id = row.asset_revision_id
            suppression.reason = "budget_eviction"
        await session.delete(row)
        evicted.append((digest, relative))
        evicted_bytes += size
    if not evicted:
        return 0, 0
    await session.flush()
    # A content-addressed file shared by a surviving row must stay on disk.
    shared_digests = set(
        (
            await session.scalars(
                select(NormalizedDerivative.content_digest).where(
                    NormalizedDerivative.content_digest.in_({digest for digest, _ in evicted})
                )
            )
        ).all()
    )
    await session.commit()
    for digest, relative in evicted:
        if digest in shared_digests:
            continue
        await asyncio.to_thread(_unlink_evicted, settings.artifact_root, digest, relative)
    return len(evicted), evicted_bytes


async def _expected_artifacts(
    session: AsyncSession,
) -> dict[str, tuple[str, int]]:
    rows = list(
        (
            await session.execute(
                select(
                    NormalizedDerivative.content_digest,
                    NormalizedDerivative.relative_path,
                    NormalizedDerivative.size_bytes,
                )
            )
        ).tuples()
    )
    return {str(digest): (str(path), int(size)) for digest, path, size in rows}


async def reconcile_storage_inventory(
    session: AsyncSession,
    settings: Settings,
) -> StorageInventory:
    """Reconcile content-addressed normalized derivatives and coordinator models."""

    started = utc_now()
    monotonic_started = time.monotonic()
    evicted_files, evicted_bytes = await _evict_cold_derivatives(session, settings, now=started)
    packages = list(
        (
            await session.execute(select(ModelPackage.relative_path, ModelPackage.size_bytes))
        ).tuples()
    )
    expected = await _expected_artifacts(session)
    # Filesystem walks can outlast PostgreSQL's 30-second idle-transaction
    # guard on a small coordinator host. Persist eviction and release the transaction before I/O.
    await session.commit()
    artifact_scan = await asyncio.to_thread(
        scan_artifact_filesystem,
        settings.artifact_root,
        expected,
        now=started,
        orphan_grace=timedelta(seconds=settings.storage_orphan_grace_seconds),
        partial_grace=timedelta(seconds=settings.storage_partial_grace_seconds),
        cleanup_candidate_limit=settings.storage_cleanup_max_files,
    )
    artifact_scan, deleted_files, deleted_bytes = await _cleanup_scan_candidates(
        session,
        root=settings.artifact_root,
        scan=artifact_scan,
        max_files=settings.storage_cleanup_max_files,
    )
    await session.commit()
    model_scan = await asyncio.to_thread(
        scan_coordinator_models,
        settings.model_root,
        packages,
    )
    completed = utc_now()
    inventory = await session.get(StorageInventory, INVENTORY_SCOPE)
    if inventory is None:
        inventory = StorageInventory(
            scope=INVENTORY_SCOPE,
            scan_started_at=started,
            scanned_at=completed,
        )
        session.add(inventory)
    inventory.scan_started_at = started
    inventory.scanned_at = completed
    inventory.scan_duration_ms = int((time.monotonic() - monotonic_started) * 1_000)
    inventory.is_complete = artifact_scan.is_complete
    inventory.artifact_logical_bytes = artifact_scan.logical_bytes
    inventory.artifact_allocated_bytes = artifact_scan.allocated_bytes
    inventory.artifact_file_count = artifact_scan.file_count
    inventory.mapped_logical_bytes = artifact_scan.mapped_logical_bytes
    inventory.mapped_allocated_bytes = artifact_scan.mapped_allocated_bytes
    inventory.mapped_file_count = artifact_scan.mapped_file_count
    inventory.orphan_logical_bytes = artifact_scan.orphan_logical_bytes
    inventory.orphan_allocated_bytes = artifact_scan.orphan_allocated_bytes
    inventory.orphan_file_count = artifact_scan.orphan_file_count
    inventory.partial_logical_bytes = artifact_scan.partial_logical_bytes
    inventory.partial_allocated_bytes = artifact_scan.partial_allocated_bytes
    inventory.partial_file_count = artifact_scan.partial_file_count
    inventory.unknown_logical_bytes = artifact_scan.unknown_logical_bytes
    inventory.unknown_allocated_bytes = artifact_scan.unknown_allocated_bytes
    inventory.unknown_file_count = artifact_scan.unknown_file_count
    inventory.missing_mapping_bytes = artifact_scan.missing_mapping_bytes
    inventory.missing_mapping_count = artifact_scan.missing_mapping_count
    inventory.mismatched_file_count = artifact_scan.mismatched_file_count
    inventory.symlink_count = artifact_scan.symlink_count
    inventory.scan_error_count = artifact_scan.error_count
    inventory.cleanup_deleted_files = deleted_files
    inventory.cleanup_deleted_bytes = deleted_bytes
    inventory.registered_model_package_bytes = model_scan.registered_package_bytes
    inventory.coordinator_model_logical_bytes = model_scan.logical_bytes
    inventory.coordinator_model_allocated_bytes = model_scan.allocated_bytes
    inventory.coordinator_model_file_count = model_scan.file_count
    inventory.coordinator_model_missing_count = model_scan.missing_count
    await session.flush()

    anomaly_count = (
        inventory.orphan_file_count
        + inventory.partial_file_count
        + inventory.unknown_file_count
        + inventory.missing_mapping_count
        + inventory.mismatched_file_count
        + inventory.symlink_count
        + inventory.scan_error_count
    )
    if deleted_files or evicted_files or anomaly_count:
        await append_operation_event(
            session,
            OperationRecord(
                source="system",
                event_type="storage.inventory.reconciled",
                message="Coordinator storage inventory was reconciled",
                severity="warning" if anomaly_count else "info",
                details={
                    "count": inventory.artifact_file_count,
                    "removed": deleted_files,
                    "evicted": evicted_files,
                    "evicted_bytes": evicted_bytes,
                    "missing": inventory.missing_mapping_count,
                    "orphan": inventory.orphan_file_count,
                    "partial": inventory.partial_file_count,
                },
            ),
        )
    return inventory


async def _database_size(session: AsyncSession) -> int:
    if session.get_bind().dialect.name != "postgresql":
        return 0
    value = await session.scalar(text("select pg_database_size(current_database())"))
    return int(value or 0)


async def _tracked_artifact_bytes(session: AsyncSession) -> int:
    value = await session.scalar(select(func.sum(NormalizedDerivative.size_bytes)))
    return int(value or 0)


def _volume_capacity(path: Path) -> tuple[int, int, int]:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    stats = os.statvfs(candidate)
    fragment_size = int(stats.f_frsize or stats.f_bsize)
    return (
        int(stats.f_blocks) * fragment_size,
        int(stats.f_bfree) * fragment_size,
        int(stats.f_bavail) * fragment_size,
    )


def _content_file_exists(root: Path, digest: str, size_bytes: int) -> bool:
    if _DIGEST.fullmatch(digest) is None:
        return False
    path = root / "sha256" / digest[:2] / digest
    try:
        file_stat = path.lstat()
    except FileNotFoundError, OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and int(file_stat.st_size) == size_bytes


async def evaluate_artifact_write_admission(
    session: AsyncSession,
    settings: Settings,
    *,
    incoming_bytes: int,
    content_digest: str | None = None,
) -> StorageAdmission:
    if incoming_bytes < 0:
        raise ValueError("incoming artifact size cannot be negative")
    tracked_bytes, database_bytes, inventory = await asyncio.gather(
        _tracked_artifact_bytes(session),
        _database_size(session),
        session.get(StorageInventory, INVENTORY_SCOPE),
    )
    actual_artifact_bytes = (
        int(inventory.artifact_allocated_bytes)
        if inventory is not None and inventory.is_complete
        else 0
    )
    artifact_accounted = max(tracked_bytes, actual_artifact_bytes)
    coordinator_models = (
        int(inventory.coordinator_model_allocated_bytes)
        if inventory is not None and inventory.is_complete
        else 0
    )
    is_existing = content_digest is not None and await asyncio.to_thread(
        _content_file_exists,
        settings.artifact_root,
        content_digest,
        incoming_bytes,
    )
    additional_bytes = 0 if is_existing else incoming_bytes
    total_bytes, _free_including_reserved, available_bytes = await asyncio.to_thread(
        _volume_capacity, settings.artifact_root
    )
    configured_reserve = int(getattr(settings, "storage_min_free_bytes", 50 * 1024**3))
    reserve_bytes = min(configured_reserve, max(0, total_bytes // 2))
    projected = artifact_accounted + coordinator_models + database_bytes + additional_bytes
    managed_limit = int(settings.managed_storage_hard_limit_bytes)
    reason: str | None = None
    if artifact_accounted + additional_bytes > int(settings.artifact_cache_max_bytes):
        reason = "artifact cache byte ceiling would be exceeded"
    elif projected > managed_limit:
        reason = "Star managed-storage safety cap would be exceeded"
    elif available_bytes - additional_bytes < reserve_bytes:
        reason = "coordinator filesystem reserve would be consumed"
    return StorageAdmission(
        allowed=reason is None,
        reason=reason,
        incoming_bytes=incoming_bytes,
        additional_bytes=additional_bytes,
        artifact_accounted_bytes=artifact_accounted,
        database_bytes=database_bytes,
        coordinator_model_bytes=coordinator_models,
        projected_managed_bytes=projected,
        managed_limit_bytes=managed_limit,
        volume_available_bytes=available_bytes,
        filesystem_reserve_bytes=reserve_bytes,
    )


async def raise_storage_admission_error(
    session: AsyncSession,
    admission: StorageAdmission,
) -> None:
    """Emit one rate-limited ENOSPC event and raise an explicit OSError."""

    cutoff = utc_now() - _ADMISSION_EVENT_COOLDOWN
    recent = await session.scalar(
        select(OperationalEvent.id)
        .where(
            OperationalEvent.event_type == "storage.write.denied",
            OperationalEvent.created_at >= cutoff,
        )
        .limit(1)
    )
    if recent is None:
        await append_operation_event(
            session,
            OperationRecord(
                source="system",
                event_type="storage.write.denied",
                message="Artifact cache write rejected before disk I/O (ENOSPC)",
                severity="critical",
                details={
                    "error_class": "StorageAdmissionError",
                    "managed_bytes": admission.projected_managed_bytes,
                    "hard_limit_bytes": admission.managed_limit_bytes,
                    "available_bytes": admission.volume_available_bytes,
                    "reserve_bytes": admission.filesystem_reserve_bytes,
                },
            ),
        )
    await session.commit()
    raise StorageAdmissionError(admission.reason or "artifact cache write was rejected")
