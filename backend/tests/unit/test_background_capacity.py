from __future__ import annotations

import uuid
from datetime import UTC, datetime

from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    CleanupCampaign,
    LibraryProgram,
    ProcessingBatch,
    Task,
    User,
)
from star.domain.enums import (
    CampaignState,
    LibraryProgramState,
    MediaType,
    ProcessingBatchState,
    Role,
    TaskState,
)
from star.services.background import (
    finalize_processing_batches,
)


async def _generation_for_batch(session, *, state: str, suffix: str) -> AnalysisGeneration:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=f"batch-{suffix}",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
        analysis_generation=1,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest=f"batch-{suffix}",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        sequence=1,
        scope={},
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="streaming-dag-v95",
        inference_profiles={},
        prompt_version="photo-observations-v1",
        schema_version="individual-photo-review-v2",
        calibration_version="absolute-rating-v1",
        state=state,
        reason="test",
    )
    session.add(generation)
    await session.flush()
    asset.current_generation_id = generation.id
    return generation


async def test_failed_required_task_marks_processing_batch_failed(session) -> None:
    owner = User(
        username="batch-owner",
        display_name="Batch Owner",
        role=Role.ADMINISTRATOR,
    )
    program = LibraryProgram(
        name="Batch finalization test",
        state=LibraryProgramState.RUNNING,
        enabled=True,
        completion_batch_size=1,
        cohort_mode="custom",
        selection_order="newest",
        requested_asset_count=1,
        admitted_asset_count=1,
        library_generation=1,
        work_scope="known",
    )
    session.add_all([owner, program])
    await session.flush()
    campaign = CleanupCampaign(
        library_program_id=program.id,
        origin="background",
        name="Failed batch",
        objective="test",
        state=CampaignState.ANALYZING,
        scope={},
        created_by=owner.id,
    )
    session.add(campaign)
    await session.flush()
    batch = ProcessingBatch(
        program_id=program.id,
        campaign_id=campaign.id,
        state=ProcessingBatchState.RUNNING,
        asset_count=1,
        pipeline_version="primary-review-v4",
    )
    session.add(batch)
    await session.flush()
    generation = await _generation_for_batch(session, state="failed", suffix="failed")
    session.add(
        Task(
            campaign_id=campaign.id,
            processing_batch_id=batch.id,
            analysis_generation_id=generation.id,
            task_type="asset.primary_review",
            routing_key="small-vlm",
            data_class="family-preview",
            estimated_work_class="long",
            target_ref={},
            requirements={},
            idempotency_key="failed-batch-review",
            state=TaskState.FAILED,
            attempt_count=2,
            max_attempts=2,
            last_error_class="LocalInferenceSchemaError",
        )
    )
    await session.flush()

    finalized = await finalize_processing_batches(session, program=program)

    assert finalized == 1
    assert batch.state == ProcessingBatchState.FAILED
    assert campaign.state == CampaignState.REVIEWING
    assert batch.checkpoint["core_task_counts"] == {"failed": 1}
    assert program.state == LibraryProgramState.ERROR
    assert program.enabled is False
    assert program.last_error_class == "RequiredAnalysisFailed"


async def test_failed_enrichment_does_not_fail_completed_core_batch(session) -> None:
    owner = User(
        username="enrichment-owner",
        display_name="Enrichment Owner",
        role=Role.ADMINISTRATOR,
    )
    program = LibraryProgram(
        name="Enrichment finalization test",
        state=LibraryProgramState.RUNNING,
        enabled=True,
        completion_batch_size=1,
        cohort_mode="custom",
        selection_order="newest",
        requested_asset_count=1,
        admitted_asset_count=1,
        library_generation=1,
    )
    session.add_all([owner, program])
    await session.flush()
    campaign = CleanupCampaign(
        library_program_id=program.id,
        origin="background",
        name="Optional evidence batch",
        objective="test",
        state=CampaignState.ANALYZING,
        scope={},
        created_by=owner.id,
    )
    session.add(campaign)
    await session.flush()
    batch = ProcessingBatch(
        program_id=program.id,
        campaign_id=campaign.id,
        state=ProcessingBatchState.RUNNING,
        asset_count=1,
        pipeline_version="model-pipeline-v1",
    )
    session.add(batch)
    await session.flush()
    generation = await _generation_for_batch(session, state="published", suffix="published")
    session.add_all(
        [
            Task(
                campaign_id=campaign.id,
                processing_batch_id=batch.id,
                analysis_generation_id=generation.id,
                task_type="asset.primary_review",
                routing_key="small-vlm",
                data_class="family-preview",
                estimated_work_class="long",
                completion_role="core",
                target_ref={},
                requirements={},
                idempotency_key="complete-core-review",
                state=TaskState.SUCCEEDED,
            ),
            Task(
                campaign_id=campaign.id,
                processing_batch_id=batch.id,
                analysis_generation_id=generation.id,
                task_type="asset.legacy_enrichment",
                routing_key="legacy-enrichment",
                data_class="family-preview",
                estimated_work_class="medium",
                completion_role="enrichment",
                target_ref={},
                requirements={},
                idempotency_key="failed-optional-signal",
                state=TaskState.FAILED,
                last_error_class="LocalModelUnavailable",
            ),
        ]
    )
    await session.flush()

    finalized = await finalize_processing_batches(session, program=program)

    assert finalized == 1
    assert batch.state == ProcessingBatchState.COMPLETE
    assert campaign.state == CampaignState.COMPLETE
    assert batch.checkpoint["core_task_counts"] == {"succeeded": 1}
    assert batch.checkpoint["enrichment_task_counts"] == {"failed": 1}
