"""One rule for whether a task still describes the photograph it was made for.

Leasing, queue repair and the task producers used to each decide this on their
own, and disagreed: publication accepted a metadata-only revision of the same
picture while leasing rejected it, so a review that had every right to run was
refused as obsolete.
"""

from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    Task,
)
from star.domain.enums import AccountPurpose, HealthStatus, MediaType, TaskState
from star.services.task_scope import task_generation_is_current


async def _photo(session) -> Asset:
    instance = ImmichInstance(
        stable_instance_id="scope", base_url="https://immich.home.arpa", health=HealthStatus.HEALTHY
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
        immich_asset_id="scoped",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    return asset


def _revision(asset: Asset, **metadata) -> AssetRevision:
    return AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest=uuid.uuid4().hex * 2,
        normalized_metadata=metadata,
        file_size=5_242_880,
        width=4032,
        height=3024,
    )


async def test_a_metadata_only_revision_keeps_the_task_current_and_new_bytes_do_not(
    session,
) -> None:
    asset = await _photo(session)
    reviewed = _revision(asset, checksum="abc", rating=3)
    session.add(reviewed)
    await session.flush()
    asset.current_revision_id = reviewed.id
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=reviewed.id,
        sequence=1,
        state="published",
        published_at=utc_now(),
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="automatic_review",
        scope={},
    )
    session.add(generation)
    await session.flush()
    asset.current_generation_id = generation.id
    advanced = Task(
        asset_id=asset.id,
        asset_revision_id=reviewed.id,
        analysis_generation_id=generation.id,
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="family-preview",
        completion_role="conditional",
        idempotency_key="advanced-follow-up",
        state=TaskState.QUEUED,
        target_ref={},
        requirements={},
    )
    session.add(advanced)
    await session.flush()
    assert await task_generation_is_current(session, advanced) is True

    # The owner rates it in Immich: same picture, new revision. Still current.
    rated = _revision(asset, checksum="abc", rating=5)
    session.add(rated)
    await session.flush()
    asset.current_revision_id = rated.id
    assert await task_generation_is_current(session, advanced) is True

    # The bytes changed: a different picture. The review no longer applies.
    replaced = _revision(asset, checksum="def", rating=5)
    session.add(replaced)
    await session.flush()
    asset.current_revision_id = replaced.id
    assert await task_generation_is_current(session, advanced) is False


async def test_every_refusal_has_a_name(session) -> None:
    """A refusal nobody can explain is how the Apple worker sat idle for days."""

    from star.services.task_scope import task_currency_reason

    asset = await _photo(session)
    asset.analysis_generation = 3
    reviewed = _revision(asset, checksum="abc")
    session.add(reviewed)
    await session.flush()
    asset.current_revision_id = reviewed.id
    superseded = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=reviewed.id,
        sequence=1,
        state="superseded",
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
    session.add(superseded)
    await session.flush()

    def task(**fields) -> Task:
        base = dict(
            asset_id=asset.id,
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="family-preview",
            completion_role="conditional",
            idempotency_key=str(uuid.uuid4()),
            state=TaskState.QUEUED,
            target_ref={},
            requirements={},
        )
        base.update(fields)
        return Task(**base)

    assert await task_currency_reason(session, task(analysis_generation_id=superseded.id)) == (
        "generation_superseded"
    )
    assert await task_currency_reason(session, task(asset_revision_id=uuid.uuid4())) == (
        "task_revision_superseded"
    )
    assert (
        await task_currency_reason(
            session, task(target_ref={"library_generation": asset.analysis_generation + 1})
        )
        == "task_from_older_intake"
    )
    assert await task_currency_reason(session, task(asset_revision_id=reviewed.id)) is None
