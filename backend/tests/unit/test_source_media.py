import uuid

import pytest

from star.db.models import Asset, ImmichConnection, ImmichInstance, Task
from star.domain.enums import (
    AccountPurpose,
    HealthStatus,
    MediaType,
    TaskState,
)
from star.services.source_media import record_permanent_source_failure


async def _asset_and_task(session) -> tuple[Asset, Task, Task]:
    instance = ImmichInstance(
        stable_instance_id="source-quarantine-instance",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
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
        immich_asset_id="broken-derivative",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    task = Task(
        connection_id=connection.id,
        asset_id=asset.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_id": str(asset.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="source-quarantine-primary",
        state=TaskState.RUNNING,
    )
    sibling = Task(
        connection_id=connection.id,
        asset_id=asset.id,
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(asset.id)},
        requirements={"capabilities": ["image-quality"]},
        idempotency_key="source-quarantine-sibling",
        state=TaskState.QUEUED,
    )
    session.add_all([task, sibling])
    await session.flush()
    return asset, task, sibling


async def test_repeated_permanent_source_failure_quarantines_only_local_asset(session) -> None:
    asset, task, sibling = await _asset_and_task(session)
    group_sibling = Task(
        connection_id=asset.connection_id,
        asset_id=None,
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_ids": [str(asset.id), str(uuid.uuid4())]},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="source-quarantine-group-sibling",
        state=TaskState.RETRY_WAITING,
    )
    session.add(group_sibling)
    await session.flush()

    first = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code="invalid_image",
    )
    second = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code="invalid_image",
    )

    assert first.quarantined is False
    assert second.quarantined is True
    assert second.cancelled_tasks == 2
    assert asset.access_state == "quarantined"
    assert asset.exclusion_reason == "unusable_immich_derivative"
    assert asset.source_failure_count == 2
    assert sibling.state == TaskState.CANCELLED
    assert sibling.last_error_class == "SourceMediaQuarantined"
    assert group_sibling.state == TaskState.CANCELLED
    assert group_sibling.last_error_class == "SourceMediaQuarantined"


async def test_blank_preview_relay_code_is_permanent(session) -> None:
    asset, task, sibling = await _asset_and_task(session)

    first = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code="immich_preview_blank_image",
    )
    second = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code="immich_preview_blank_image",
    )

    assert first.quarantined is False
    assert second.quarantined is True
    assert asset.access_state == "quarantined"
    assert sibling.state == TaskState.CANCELLED


async def test_transient_source_failure_never_quarantines(session) -> None:
    asset, task, _sibling = await _asset_and_task(session)

    for _ in range(4):
        outcome = await record_permanent_source_failure(
            session,
            task=task,
            source_asset_id=asset.id,
            source_error_code="immich_unavailable",
        )

    assert outcome.quarantined is False
    assert asset.access_state == "available"
    assert asset.source_failure_count == 0


@pytest.mark.parametrize(
    "code", ["oversized", "pixel_limit", "immich_preview_oversized", "immich_preview_pixel_limit"]
)
async def test_source_limits_quarantine_after_two_checks_across_metadata_refresh(session, code):
    asset, task, _ = await _asset_and_task(session)
    await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code=code,
    )
    # A metadata revision change must not buy another two attempts.
    asset.source_failure_revision_id = uuid.uuid4()
    result = await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code=code,
    )
    assert result.quarantined
    assert asset.source_failure_count == 2
    await record_permanent_source_failure(
        session,
        task=task,
        source_asset_id=asset.id,
        source_error_code=code,
    )
    assert asset.source_failure_count == 2


async def test_quarantine_cancels_only_tasks_referencing_the_asset(session) -> None:
    asset, task, sibling = await _asset_and_task(session)
    unrelated = Task(
        connection_id=asset.connection_id,
        asset_id=None,
        task_type="group.cohort_triage",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_ids": [str(uuid.uuid4()), str(uuid.uuid4())]},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="source-quarantine-unrelated",
        state=TaskState.QUEUED,
    )
    session.add(unrelated)
    await session.flush()

    for _ in range(2):
        outcome = await record_permanent_source_failure(
            session,
            task=task,
            source_asset_id=asset.id,
            source_error_code="blank_image",
        )

    assert outcome.quarantined is True
    assert sibling.state == TaskState.CANCELLED
    assert unrelated.state == TaskState.QUEUED
    assert unrelated.last_error_class is None
