from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from star.db.base import utc_now
from star.db.models import AnalysisNode, Asset, AssetRevision, Task
from star.domain.enums import MediaType, TaskState
from star.services.analysis_generations import (
    bind_generation_task,
    create_analysis_generation,
)
from star.services.task_recovery import reconcile_task_state_invariants


async def _generation_fixture(session, *, suffix: str):
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=f"invariant-{suffix}",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest=f"invariant-revision-{suffix}",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="automatic_review",
        mode="complete_review",
        start_stage="technical",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"kind": "test"},
    )
    return asset, revision, generation


async def test_invariant_reconciler_returns_orphaned_running_task_to_queue(session) -> None:
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="orphaned-running-invariant",
        state=TaskState.RUNNING,
        attempt_count=1,
        max_attempts=3,
        lease_id=uuid.uuid4(),
        # A running row without all three lease coordinates is impossible to
        # submit safely and must be recovered without consuming model compute.
        leased_to_worker_id=None,
        lease_expires_at=utc_now() + timedelta(minutes=1),
    )
    session.add(task)
    await session.flush()

    assert await reconcile_task_state_invariants(session, limit=10) == 1
    assert task.state == TaskState.RETRY_WAITING
    assert task.attempt_count == 0
    assert task.lease_id is None
    assert task.lease_expires_at is None
    assert task.progress["state_invariant_recovery_count"] == 1


async def test_invariant_reconciler_cancels_work_for_terminal_generation(session) -> None:
    asset, revision, generation = await _generation_fixture(session, suffix="cancelled")
    task = Task(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_generation_id=generation.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="terminal-generation-active-task",
        state=TaskState.QUEUED,
    )
    session.add(task)
    await session.flush()
    await bind_generation_task(
        session,
        generation_id=generation.id,
        stage="primary",
        task=task,
        required=True,
    )
    generation.state = "cancelled"
    generation.completed_at = utc_now()

    assert await reconcile_task_state_invariants(session, limit=10) == 1
    assert task.state == TaskState.CANCELLED
    assert task.last_error_class == "GenerationNoLongerRunnable"
    node = await session.scalar(
        select(AnalysisNode).where(
            AnalysisNode.generation_id == generation.id,
            AnalysisNode.stage_key == "primary",
        )
    )
    assert node is not None
    assert node.state == "cancelled"


async def test_invariant_reconciler_bounds_repeated_internal_recovery(session) -> None:
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="repeated-invariant-failure",
        state=TaskState.RUNNING,
        attempt_count=2,
        max_attempts=3,
        progress={"state_invariant_recovery_count": 3},
    )
    session.add(task)
    await session.flush()

    assert await reconcile_task_state_invariants(session, limit=10) == 1
    assert task.state == TaskState.FAILED
    assert task.last_error_class == "RepeatedTaskStateInvariantFailure"
    assert task.progress["state_invariant_recovery_count"] == 4
