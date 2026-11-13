"""Opt-in read-path budget against a large synthetic, disposable library."""

import os
import time

import pytest
from sqlalchemy import String, cast, func, insert, literal, select, text
from test_postgres_runtime import _settings, _worker
from test_postgres_runtime import postgres_sessions as postgres_sessions

from star.api.routers.workers.management import list_workers
from star.db.models import Asset, ImmichConnection, ImmichInstance, Task
from star.domain.enums import AccountPurpose, MediaType, TaskState


async def test_progress_aggregate_over_50k_photos_and_500k_retained_tasks(postgres_sessions):
    import uuid

    from sqlalchemy import Integer, case, update

    from star.db.models import (
        AnalysisGeneration,
        AssetRevision,
        LibraryProgram,
        WorkRun,
        WorkRunAsset,
    )
    from star.services.run_summary import _run_counts

    async with postgres_sessions() as session:
        instance = ImmichInstance(stable_instance_id=str(uuid.uuid4()), base_url="http://127.0.0.1")
        program = LibraryProgram(name=f"progress-scale-{uuid.uuid4()}")
        session.add_all([instance, program])
        await session.flush()
        connection = ImmichConnection(
            instance_id=instance.id,
            immich_user_id="scale",
            display_label="Synthetic",
            purpose=AccountPurpose.OTHER,
            processing_profile="default",
            model_profile="default",
        )
        session.add(connection)
        await session.flush()
        run = WorkRun(id=uuid.uuid4(), program_id=program.id, library_generation=1, requested=50000)
        session.add(run)
        await session.flush()
        numbers = func.generate_series(1, 50000).table_valued("value").render_derived()
        await session.execute(
            insert(Asset).from_select(
                [
                    "id",
                    "instance_id",
                    "connection_id",
                    "immich_asset_id",
                    "immich_owner_user_id",
                    "media_type",
                ],
                select(
                    func.gen_random_uuid(),
                    literal(instance.id),
                    literal(connection.id),
                    cast(numbers.c.value, String),
                    literal("synthetic"),
                    literal(MediaType.IMAGE, type_=Asset.media_type.type),
                ).select_from(numbers),
            )
        )
        scope = Asset.connection_id == connection.id
        await session.execute(
            insert(AssetRevision).from_select(
                ["id", "asset_id", "immich_updated_at", "metadata_digest", "normalized_metadata"],
                select(
                    Asset.id,
                    Asset.id,
                    func.now(),
                    cast(Asset.id, String),
                    literal({}, type_=AssetRevision.normalized_metadata.type),
                ).where(scope),
            )
        )
        building = cast(Asset.immich_asset_id, Integer) % 10 == 0
        await session.execute(
            insert(AnalysisGeneration).from_select(
                [
                    "id",
                    "asset_id",
                    "asset_revision_id",
                    "sequence",
                    "start_stage",
                    "model_policy_version",
                    "prompt_version",
                    "schema_version",
                    "calibration_version",
                    "reason",
                    "state",
                    "published_at",
                ],
                select(
                    Asset.id,
                    Asset.id,
                    Asset.id,
                    literal(1),
                    literal("source"),
                    literal("test"),
                    literal("test"),
                    literal("test"),
                    literal("test"),
                    literal("scale"),
                    case((building, "building"), else_="published"),
                    case((building, None), else_=func.now()),
                ).where(scope),
            )
        )
        await session.execute(
            update(Asset)
            .where(scope)
            .values(current_revision_id=Asset.id, current_generation_id=Asset.id)
        )
        await session.execute(
            insert(WorkRunAsset).from_select(
                ["run_id", "asset_id", "asset_revision_id", "generation_id"],
                select(literal(run.id), Asset.id, Asset.id, Asset.id).where(scope),
            )
        )
        copies = func.generate_series(1, 10).table_valued("value").render_derived()
        await session.execute(
            insert(Task).from_select(
                [
                    "id",
                    "asset_id",
                    "analysis_generation_id",
                    "task_type",
                    "state",
                    "target_ref",
                    "requirements",
                    "idempotency_key",
                ],
                select(
                    func.gen_random_uuid(),
                    Asset.id,
                    Asset.id,
                    literal("asset.primary_review"),
                    literal(TaskState.SUCCEEDED, type_=Task.state.type),
                    literal({}, type_=Task.target_ref.type),
                    literal({}, type_=Task.requirements.type),
                    func.concat(cast(Asset.id, String), "-", copies.c.value),
                )
                .select_from(Asset)
                .join(copies, literal(True))
                .where(scope),
            )
        )
        await session.commit()
        for table in ("tasks", "assets", "analysis_generations", "work_run_assets"):
            await session.execute(text(f"ANALYZE {table}"))
        await session.commit()
        durations = []
        for _ in range(5):
            started = time.perf_counter()
            assert await _run_counts(session, run.id) == (50000, 45000, 0, 0)
            durations.append(time.perf_counter() - started)
        assert max(durations) < 3, f"Progress aggregate too slow: {durations}"
        print(
            f"50k members + 500k retained tasks: slowest progress aggregate {max(durations):.3f}s"
        )


pytestmark = pytest.mark.skipif(
    os.getenv("STAR_POSTGRES_LOAD_TEST") != "1", reason="Opt-in 450k-photo scale test"
)


async def test_light_worker_reads_do_not_scan_450k_photos_and_history(postgres_sessions):
    settings = _settings()
    async with postgres_sessions() as session:
        instance = ImmichInstance(stable_instance_id="synthetic-scale", base_url="http://127.0.0.1")
        worker = _worker("scale-worker", "scale-test-token", settings)
        session.add_all([instance, worker])
        await session.flush()
        connection = ImmichConnection(
            instance_id=instance.id,
            immich_user_id="synthetic",
            display_label="Synthetic",
            purpose=AccountPurpose.OTHER,
            processing_profile="default",
            model_profile="default",
        )
        session.add(connection)
        await session.flush()
        numbers = func.generate_series(1, 450000).table_valued("value").render_derived()
        await session.execute(
            insert(Asset).from_select(
                [
                    "id",
                    "instance_id",
                    "connection_id",
                    "immich_asset_id",
                    "immich_owner_user_id",
                    "media_type",
                ],
                select(
                    func.gen_random_uuid(),
                    literal(instance.id),
                    literal(connection.id),
                    cast(numbers.c.value, String),
                    literal("synthetic"),
                    literal(MediaType.IMAGE, type_=Asset.media_type.type),
                ).select_from(numbers),
            )
        )
        await session.execute(
            insert(Task).from_select(
                [
                    "id",
                    "asset_id",
                    "task_type",
                    "state",
                    "target_ref",
                    "requirements",
                    "idempotency_key",
                ],
                select(
                    func.gen_random_uuid(),
                    Asset.id,
                    literal("asset.primary_review"),
                    literal(TaskState.SUCCEEDED, type_=Task.state.type),
                    literal({}, type_=Task.target_ref.type),
                    literal({}, type_=Task.requirements.type),
                    cast(Asset.id, String),
                ).where(Asset.connection_id == connection.id),
            )
        )
        await session.commit()
        await session.execute(text("ANALYZE assets"))
        await session.execute(text("ANALYZE tasks"))
        await session.commit()
    latencies = []
    for _ in range(20):
        async with postgres_sessions() as session:
            started = time.perf_counter()
            rows = await list_workers(metrics="summary", session=session, settings=settings)
            latencies.append(time.perf_counter() - started)
            assert len(rows) == 1
    p95 = sorted(latencies)[18]
    assert p95 < 0.5, f"Light worker read p95 exceeded 500 ms: {p95 * 1000:.1f} ms"
    print(f"Synthetic 450000 assets + 450000 retained tasks: worker read p95={p95 * 1000:.1f} ms")
