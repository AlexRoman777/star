"""History that describes nothing is removed; everything that does is kept."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    OperationalEvent,
    Proposal,
    Task,
)
from star.domain.enums import (
    AccountPurpose,
    HealthStatus,
    MediaType,
    ProposalState,
    ProposalType,
    TaskState,
)
from star.services.history_pruning import prune_history
from star.services.operations import prune_operation_events


async def _photo(session) -> Asset:
    instance = ImmichInstance(
        stable_instance_id="prune", base_url="https://immich.home.arpa", health=HealthStatus.HEALTHY
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="pruned",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="p" * 64,
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return asset


def _generation(
    asset: Asset, *, sequence: int, state: str, superseded_at=None
) -> AnalysisGeneration:
    return AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        sequence=sequence,
        state=state,
        superseded_at=superseded_at,
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="continuous_contract_refresh",
        scope={},
    )


def _node(generation: AnalysisGeneration, asset: Asset) -> AnalysisNode:
    return AnalysisNode(
        generation_id=generation.id,
        asset_id=asset.id,
        stage_key="primary",
        analyzer_version="test",
        inference_profile="test",
        input_digest="d" * 64,
        state="succeeded",
        required=True,
        applicable=True,
    )


def _task(asset: Asset, *, state: TaskState, key: str) -> Task:
    return Task(
        asset_id=asset.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key=key,
        state=state,
        target_ref={},
        requirements={},
    )


async def test_old_superseded_work_goes_and_everything_current_stays(session) -> None:
    asset = await _photo(session)
    long_ago = utc_now() - timedelta(days=10)
    old = _generation(asset, sequence=1, state="superseded", superseded_at=long_ago)
    fresh = _generation(asset, sequence=2, state="superseded", superseded_at=utc_now())
    published = _generation(asset, sequence=3, state="published")
    session.add_all([old, fresh, published])
    await session.flush()
    asset.current_generation_id = published.id
    session.add_all([_node(old, asset), _node(published, asset)])
    cancelled_old = _task(asset, state=TaskState.CANCELLED, key="cancelled-old")
    cancelled_new = _task(asset, state=TaskState.CANCELLED, key="cancelled-new")
    succeeded_old = _task(asset, state=TaskState.SUCCEEDED, key="succeeded-old")
    session.add_all([cancelled_old, cancelled_new, succeeded_old])
    await session.flush()
    cancelled_old.updated_at = long_ago
    succeeded_old.updated_at = long_ago
    await session.flush()

    outcome = await prune_history(session, older_than_days=3)
    assert outcome["superseded_generations"] == 1
    assert outcome["analysis_nodes"] == 1
    assert outcome["cancelled_tasks"] == 1

    remaining = set(await session.scalars(select(AnalysisGeneration.id)))
    assert remaining == {fresh.id, published.id}, "the fresh and the current survive"
    assert await session.scalar(select(func.count(AnalysisNode.id))) == 1
    kept = set(await session.scalars(select(Task.idempotency_key)))
    assert kept == {"cancelled-new", "succeeded-old"}, "only old cancelled work is removed"

    # Idempotent: nothing left to prune.
    again = await prune_history(session, older_than_days=3)
    assert again["superseded_generations"] == 0 and again["cancelled_tasks"] == 0


async def test_event_pruning_is_bounded_per_call_and_keeps_the_newest(session) -> None:
    """A 1.6-million-row backlog drains over a few ticks, never in one lock."""

    for index in range(30):
        session.add(
            OperationalEvent(
                event_id=uuid.uuid4(),
                severity="info",
                source="test",
                event_type="test.event",
                message=f"event {index}",
                details={},
            )
        )
    await session.flush()
    removed = await prune_operation_events(
        session, retention_days=30, max_rows=10, batch_size=5, max_batches=2
    )
    assert removed == 10, "two batches of five, not the whole backlog at once"
    assert await session.scalar(select(func.count(OperationalEvent.id))) == 20
    removed = await prune_operation_events(
        session, retention_days=30, max_rows=10, batch_size=5, max_batches=10
    )
    assert removed == 10
    assert await session.scalar(select(func.count(OperationalEvent.id))) == 10
    newest = list(
        await session.scalars(select(OperationalEvent.message).order_by(OperationalEvent.id))
    )
    assert newest[0] == "event 20" and newest[-1] == "event 29"


def _run(
    asset: Asset,
    *,
    analysis_type: str,
    generation_id=None,
    started_at=None,
    processor_version: str = "test",
) -> AnalysisRun:
    return AnalysisRun(
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        analysis_generation_id=generation_id,
        analysis_type=analysis_type,
        processor_version=processor_version,
        model_key="test-model",
        status="succeeded",
        limitations=[],
        input_provenance={},
        result_payload={"answer": "x" * 64},
        started_at=started_at or utc_now(),
    )


def _proposal(asset: Asset, *, generation_id, created_at=None) -> Proposal:
    proposal = Proposal(
        campaign_id=uuid.uuid4(),
        analysis_generation_id=generation_id,
        proposal_type=ProposalType.RATING,
        target_type="asset",
        target_id=asset.id,
        value={"rating": 4, "source": "primary_photo_review"},
        confidence=0.8,
        reason_codes=[],
        explanation="test",
        state=ProposalState.PENDING,
        analysis_version="test",
    )
    if created_at is not None:
        proposal.created_at = created_at
    return proposal


async def test_generations_a_run_still_counts_are_never_pruned(session) -> None:
    """Incident: pruning a run's generation made the run count the photo as unfinished."""

    from star.db.models import WorkRunAsset

    asset = await _photo(session)
    long_ago = utc_now() - timedelta(days=10)
    counted = _generation(asset, sequence=1, state="superseded", superseded_at=long_ago)
    forgotten = _generation(asset, sequence=2, state="superseded", superseded_at=long_ago)
    published = _generation(asset, sequence=3, state="published")
    session.add_all([counted, forgotten, published])
    await session.flush()
    asset.current_generation_id = published.id
    session.add(
        WorkRunAsset(
            run_id=uuid.uuid4(),
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            generation_id=counted.id,
        )
    )
    await session.flush()

    outcome = await prune_history(session, older_than_days=3)

    assert outcome["superseded_generations"] == 1
    remaining = set(await session.scalars(select(AnalysisGeneration.id)))
    assert remaining == {counted.id, published.id}, "the run's generation survives, the other goes"
