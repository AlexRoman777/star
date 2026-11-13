from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from star.db.models import ModelVersion
from star.domain.schemas import ModelPackageCreate

ALLOWED_MODEL_SOURCE_HOSTS = frozenset({"huggingface.co", "registry.ollama.com"})
MUTABLE_REVISIONS = frozenset({"main", "master", "latest", "stable"})


def validate_model_source(payload: ModelPackageCreate, model: ModelVersion) -> None:
    parsed = urlsplit(str(payload.source_url))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_MODEL_SOURCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("model source must use an approved HTTPS registry host")
    if payload.source_revision.lower() in MUTABLE_REVISIONS:
        raise ValueError("model source revision must be immutable")
    if model.artifact_checksum != payload.artifact_checksum:
        raise ValueError("package checksum must match the registered model version")
    if model.license_id is None or model.license_id != payload.license_id:
        raise ValueError("package license must match the registered model version")


def resolve_model_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("model package path must stay inside the model store")
    root_resolved = root.resolve()
    candidate = (root / relative).resolve()
    if root_resolved not in candidate.parents or candidate.is_symlink():
        raise ValueError("model package path escaped the model store")
    return candidate


def _digest_file(path: Path) -> tuple[str, int]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("model package is not a regular file")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return "sha256:" + digest.hexdigest(), size


async def verify_model_package(
    *,
    root: Path,
    payload: ModelPackageCreate,
    model: ModelVersion,
) -> Path:
    validate_model_source(payload, model)
    path = resolve_model_path(root, payload.relative_path)
    checksum, size = await asyncio.to_thread(_digest_file, path)
    if checksum != payload.artifact_checksum:
        raise ValueError("model package checksum verification failed")
    if size != payload.size_bytes:
        raise ValueError("model package size verification failed")
    return path
