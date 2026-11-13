from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    Task,
)
from star.domain.enums import (
    AccountPurpose,
    HealthStatus,
    MediaType,
    TaskState,
)
from star.services.analysis_quarantine import (
    quarantine_repeated_primary_failure,
)


async def _primary_failure(session, *, attempts: int = 2) -> tuple[Asset, Task, Task, Task]:
    instance = ImmichInstance(
        stable_instance_id="analysis-quarantine-instance",
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
        immich_asset_id="repeated-primary-failure",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="a" * 64,
        normalized_metadata={"originalFileName": "broken-review.jpg"},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        sequence=1,
        scope={"kind": "library", "library_generation": 1},
        start_stage="technical",
        expanded_stages=["technical", "primary_review"],
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="library_review",
    )
    session.add(generation)
    await session.flush()
    failed = Task(
        connection_id=connection.id,
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_generation_id=generation.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_id": str(asset.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="analysis-quarantine-primary",
        state=TaskState.FAILED,
        attempt_count=attempts,
        max_attempts=2,
        last_error_class="LocalInferenceSchemaError",
    )
    sibling = Task(
        connection_id=connection.id,
        asset_id=asset.id,
        asset_revision_id=revision.id,
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(asset.id)},
        requirements={"capabilities": ["image-quality"]},
        idempotency_key="analysis-quarantine-sibling",
        state=TaskState.QUEUED,
    )
    group = Task(
        connection_id=connection.id,
        task_type="group.cohort_triage",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_ids": [str(asset.id)]},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="analysis-quarantine-group",
        state=TaskState.RETRY_WAITING,
    )
    session.add_all([failed, sibling, group])
    await session.flush()
    return asset, failed, sibling, group


async def test_repeated_primary_failure_quarantines_photo_and_all_sibling_work(session) -> None:
    asset, failed, sibling, group = await _primary_failure(session)

    outcome = await quarantine_repeated_primary_failure(
        session,
        task=failed,
        error_class="LocalInferenceSchemaError",
    )

    assert outcome.quarantined is True
    assert outcome.cancelled_tasks == 2
    assert asset.access_state == "quarantined"
    assert asset.exclusion_reason == "repeated_primary_failure"
    assert asset.analysis_failure_count == 2
    assert failed.state == TaskState.CANCELLED
    assert sibling.state == TaskState.CANCELLED
    assert group.state == TaskState.CANCELLED


async def test_one_primary_failure_is_not_enough_to_quarantine(session) -> None:
    asset, failed, _sibling, _group = await _primary_failure(session, attempts=1)

    outcome = await quarantine_repeated_primary_failure(
        session,
        task=failed,
        error_class="LocalInferenceSchemaError",
    )

    assert outcome.quarantined is False
    assert asset.access_state == "available"


async def test_transient_primary_failure_never_quarantines(session) -> None:
    asset, failed, _sibling, _group = await _primary_failure(session)
    failed.last_error_class = "LocalInferenceTransportError"

    outcome = await quarantine_repeated_primary_failure(
        session,
        task=failed,
        error_class="LocalInferenceTransportError",
    )

    assert outcome.quarantined is False
    assert asset.access_state == "available"
