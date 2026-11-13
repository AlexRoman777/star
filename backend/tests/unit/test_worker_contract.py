from __future__ import annotations

from star.services.pipeline_contract import LEASE_TASK_CONTRACTS
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
    compatible_task_types,
    task_contract_gaps,
    task_lease_contract,
    worker_contract_hash,
)


def test_every_leasable_task_has_an_executor_revision() -> None:
    assert WORKER_PROTOCOL_VERSION == 2
    assert {"asset.primary_review", "asset.deep_review"}.issubset(LEASE_TASK_CONTRACTS)
    # Retired individual review task types must not linger in the executor
    # registry: the contract hash and compatibility checks derive from it.
    assert not {
        "asset.fast_rating",
        "asset.apple_order_check",
        "asset.deep_review_experiment",
    }.intersection(LEASE_TASK_CONTRACTS)
    assert set(TASK_EXECUTOR_REVISIONS) == set(LEASE_TASK_CONTRACTS)
    assert worker_contract_hash(TASK_EXECUTOR_REVISIONS) == WORKER_CONTRACT_HASH


def test_worker_contract_limits_old_agents_to_the_tasks_they_support() -> None:
    advertised = dict(TASK_EXECUTOR_REVISIONS)
    advertised.pop("group.highlight_compare")
    advertised["asset.primary_review"] = 0

    compatible = compatible_task_types(advertised)
    missing, outdated = task_contract_gaps(
        advertised,
        task_types={"group.highlight_compare", "asset.primary_review", "asset.media_stage"},
    )

    assert "asset.media_stage" in compatible
    assert "group.highlight_compare" not in compatible
    assert "asset.primary_review" not in compatible
    assert missing == ["asset.primary_review", "group.highlight_compare"]
    assert outdated == []


def test_task_lease_contract_is_explicit_and_stable() -> None:
    assert task_lease_contract("asset.primary_review") == {
        "protocol_version": 2,
        "task_type": "asset.primary_review",
        # v181: star-rating contract, fallback runtime envelope, varied retry seed.
        "minimum_revision": 4,
    }
    assert task_lease_contract("group.highlight_compare") == {
        "protocol_version": 2,
        "task_type": "group.highlight_compare",
        # v181: the owner taste profile is fetched for every Highlight comparison.
        "minimum_revision": 4,
    }
