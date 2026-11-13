from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from star.api.routers.processing import _STAGE_TASK_TYPES
from star.api.routers.system import _system_alerts
from star.api.routers.workers._shared import (
    _task_generation_is_current,
    _worker_protocol_is_compatible,
    _worker_release_is_compatible,
)
from star.api.routers.workers.completion import _settle_failed_highlight_comparison
from star.db.base import utc_now
from star.domain.enums import TaskState
from star.domain.schemas import TaskCountsResponse
from star.services.highlight_league import (
    HIGHLIGHT_MODEL_KEY,
    _reported_model_matches,
)
from star.services.library_program import PIPELINE_STAGE_TASK_TYPES


@pytest.mark.parametrize(
    ("worker_release", "expected_release", "compatible"),
    [
        ("v91", "v91", True),
        ("v90", "v91", False),
        (" v91 ", "v91", True),
        ("", "v91", False),
        ("unversioned", "v91", False),
        (None, "v91", False),
        (91, "v91", False),
        ("v91", "", True),
        ("v91", "unversioned", True),
    ],
)
def test_worker_release_compatibility(
    worker_release: object,
    expected_release: str,
    compatible: bool,
) -> None:
    assert _worker_release_is_compatible(worker_release, expected_release) is compatible


@pytest.mark.parametrize(
    ("worker_protocol", "minimum", "maximum", "compatible"),
    [
        (1, 1, 1, True),
        (1, 1, 2, True),
        (2, 1, 2, True),
        (3, 1, 2, False),
        ("2", 1, 2, True),
        (None, 1, 1, False),
        (0, 1, 2, False),
    ],
)
def test_worker_protocol_compatibility(
    worker_protocol: object,
    minimum: int,
    maximum: int,
    compatible: bool,
) -> None:
    assert _worker_protocol_is_compatible(worker_protocol, minimum, maximum) is compatible


@pytest.mark.parametrize(
    ("reported", "matches"),
    [
        (HIGHLIGHT_MODEL_KEY, True),
        (f"{HIGHLIGHT_MODEL_KEY}@sha256:123", True),
        (f" {HIGHLIGHT_MODEL_KEY} ", True),
        ("", True),
        (None, True),
        ("qwen/not-the-highlight-model", False),
        (f"prefix-{HIGHLIGHT_MODEL_KEY}", False),
        (f"{HIGHLIGHT_MODEL_KEY}-lookalike", False),
    ],
)
def test_reported_highlight_model_identity(reported: object, matches: bool) -> None:
    assert _reported_model_matches(HIGHLIGHT_MODEL_KEY, reported) is matches


def _healthy_storage() -> SimpleNamespace:
    return SimpleNamespace(
        status="healthy",
        volume_free_bytes=100 * 1024**3,
        inventory_complete=True,
        orphan_artifact_file_count=0,
        partial_artifact_file_count=0,
        unknown_artifact_file_count=0,
        missing_artifact_file_count=0,
        mismatched_artifact_file_count=0,
        artifact_symlink_count=0,
        inventory_error_count=0,
    )


def _alerts(**overrides: Any) -> list[str]:
    values: dict[str, Any] = {
        "storage": _healthy_storage(),
        "stalled_tasks": 0,
        "out_of_memory_failures": 0,
        "unhealthy_connections": 0,
        "unhealthy_models": 0,
        "errors_last_five_minutes": 0,
        "oldest_queued_seconds": None,
        "workers_online": 4,
        "worker_release_mismatches": 0,
        "recent_service_restarts": 0,
    }
    values.update(overrides)
    return [alert.code for alert in _system_alerts(**values)]


def test_task_counts_response_names_its_durable_and_current_run_scopes() -> None:
    response = TaskCountsResponse(
        current_run_generation=17,
        current_run_counts={"queued": 3},
        counts={"queued": 9},
    )
    assert response.scope == "all_durable_tasks"
    assert response.current_run_generation == 17
    assert response.current_run_counts == {"queued": 3}
    assert response.counts == {"queued": 9}


def test_processing_and_control_stage_maps_remain_aligned() -> None:
    assert _STAGE_TASK_TYPES == PIPELINE_STAGE_TASK_TYPES
    assert _STAGE_TASK_TYPES["highlight_league"] == frozenset({"group.highlight_compare"})
    assert "quick_rating" not in _STAGE_TASK_TYPES
    assert _STAGE_TASK_TYPES["ministral"] == frozenset({"asset.primary_review"})
    assert _STAGE_TASK_TYPES["senior_review"] == frozenset({"asset.deep_review"})
    assert "manual_advisory" not in _STAGE_TASK_TYPES
    assert "qwen" not in _STAGE_TASK_TYPES


class _GetSession:
    def __init__(self, value: object) -> None:
        self.value = value

    async def get(self, _model: object, _identifier: object) -> object:
        return self.value


@pytest.mark.asyncio
async def test_generation_guard_rejects_stale_automatic_work() -> None:
    asset_id = uuid4()
    task = SimpleNamespace(
        asset_id=asset_id,
        target_ref={"library_generation": 8},
    )
    current = _GetSession(SimpleNamespace(id=asset_id, analysis_generation=8))
    stale = _GetSession(SimpleNamespace(id=asset_id, analysis_generation=9))
    missing = _GetSession(None)

    assert await _task_generation_is_current(current, task) is True  # type: ignore[arg-type]
    assert await _task_generation_is_current(stale, task) is False  # type: ignore[arg-type]
    assert await _task_generation_is_current(missing, task) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["manual", "user", "experiment"])
async def test_generation_guard_preserves_explicit_work(origin: str) -> None:
    task = SimpleNamespace(
        asset_id=uuid4(),
        target_ref={"queue_origin": origin, "library_generation": 1},
    )
    session = _GetSession(SimpleNamespace(analysis_generation=99))
    assert await _task_generation_is_current(session, task) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_highlight_task_settles_its_comparison() -> None:
    task_id = uuid4()
    comparison_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        task_type="group.highlight_compare",
        target_ref={"comparison_id": str(comparison_id)},
    )
    comparison = SimpleNamespace(
        task_id=task_id,
        state="running",
        completed_at=None,
        explanation=None,
    )

    await _settle_failed_highlight_comparison(
        _GetSession(comparison),  # type: ignore[arg-type]
        task,  # type: ignore[arg-type]
        TaskState.FAILED.value,
    )

    assert comparison.state == TaskState.FAILED.value.lower()
    assert comparison.completed_at is not None
    assert comparison.explanation == "The local comparison did not complete."


@pytest.mark.asyncio
async def test_retrying_highlight_task_returns_its_comparison_to_queue() -> None:
    task_id = uuid4()
    comparison_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        task_type="group.highlight_compare",
        target_ref={"comparison_id": str(comparison_id)},
    )
    comparison = SimpleNamespace(
        task_id=task_id,
        state="running",
        completed_at=utc_now(),
        explanation="The local comparison did not complete.",
    )

    await _settle_failed_highlight_comparison(
        _GetSession(comparison),  # type: ignore[arg-type]
        task,  # type: ignore[arg-type]
        TaskState.RETRY_WAITING.value,
    )

    assert comparison.state == "queued"
    assert comparison.completed_at is None
    assert comparison.explanation is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "state", "comparison_id"),
    [
        ("asset.deep_review", TaskState.FAILED.value, "0191c4f0-5e5a-7c3a-9d8e-4b2a6f1e0c11"),
        ("group.highlight_compare", TaskState.FAILED.value, "invalid"),
        ("group.highlight_compare", TaskState.FAILED.value, None),
    ],
)
async def test_highlight_failure_settlement_ignores_unrelated_or_nonterminal_work(
    task_type: str,
    state: str,
    comparison_id: object,
) -> None:
    comparison = SimpleNamespace(
        task_id=uuid4(),
        state="running",
        completed_at=None,
        explanation=None,
    )
    task = SimpleNamespace(
        id=uuid4(),
        task_type=task_type,
        target_ref={"comparison_id": comparison_id},
    )

    await _settle_failed_highlight_comparison(
        _GetSession(comparison),  # type: ignore[arg-type]
        task,  # type: ignore[arg-type]
        state,
    )

    assert comparison.state == "running"
    assert comparison.completed_at is None
