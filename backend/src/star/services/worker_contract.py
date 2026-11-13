from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from star.services.pipeline_contract import LEASE_TASK_CONTRACTS

# This version describes the worker/control-plane wire envelope. It changes only
# when an older agent cannot safely communicate with the current coordinator.
WORKER_PROTOCOL_VERSION = 2

# Revisions describe executable task behaviour, independently of the Star
# release number. Bump only the affected task when its input, output, media,
# cancellation, or execution contract changes. An older worker can continue to
# lease every other task whose revision it still satisfies.
TASK_EXECUTOR_REVISIONS: dict[str, int] = {
    task_type: 1 for task_type in sorted(LEASE_TASK_CONTRACTS)
}
TASK_EXECUTOR_REVISIONS["group.highlight_compare"] = 2
TASK_EXECUTOR_REVISIONS["group.series_select"] = 2
# v181: star-rating contract, fallback runtime envelopes, varied retry seeds.
TASK_EXECUTOR_REVISIONS["asset.primary_review"] = 3
# v181: owner taste profile fetched from the coordinator for every rating request.
TASK_EXECUTOR_REVISIONS["group.highlight_compare"] = 3
# Verified manifests are required only on the model execution contracts.
for _model_task in (
    "asset.primary_review",
    "asset.deep_review",
    "group.highlight_compare",
    "group.series_select",
):
    TASK_EXECUTOR_REVISIONS[_model_task] += 1


def worker_contract_hash(task_revisions: Mapping[str, int] | None = None) -> str:
    payload = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "tasks": dict(sorted((task_revisions or TASK_EXECUTOR_REVISIONS).items())),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


WORKER_CONTRACT_HASH = worker_contract_hash()


def normalize_task_revisions(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for task_type, revision in value.items():
        if (
            isinstance(task_type, str)
            and task_type in LEASE_TASK_CONTRACTS
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and 1 <= revision <= 10_000
        ):
            normalized[task_type] = revision
    return normalized


def required_task_revision(task_type: str) -> int:
    return TASK_EXECUTOR_REVISIONS.get(task_type, 1)


def compatible_task_types(value: object) -> set[str]:
    advertised = normalize_task_revisions(value)
    return {
        task_type
        for task_type, required in TASK_EXECUTOR_REVISIONS.items()
        if advertised.get(task_type, 0) >= required
    }


def task_contract_gaps(value: object, *, task_types: set[str]) -> tuple[list[str], list[str]]:
    advertised = normalize_task_revisions(value)
    missing = sorted(task_type for task_type in task_types if task_type not in advertised)
    outdated = sorted(
        task_type
        for task_type in task_types
        if task_type in advertised and advertised[task_type] < required_task_revision(task_type)
    )
    return missing, outdated


def task_lease_contract(task_type: str) -> dict[str, object]:
    return {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "task_type": task_type,
        "minimum_revision": required_task_revision(task_type),
    }
