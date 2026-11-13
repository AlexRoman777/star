import uuid

from star.db.base import utc_now
from star.db.models import LibraryProgram, Task
from star.domain.enums import LibraryProgramState, TaskState
from star.services.library_program import (
    clear_library_queue,
    hard_stop_library_work,
    transition_library_program,
)


def program() -> LibraryProgram:
    return LibraryProgram(
        name="Full library",
        state=LibraryProgramState.DISABLED,
        enabled=False,
        revision=4,
        completion_batch_size=10,
        cohort_mode="automatic",
        selection_order="newest",
        ingest_new_assets=False,
        auto_discover_new_assets=False,
        admitted_asset_count=0,
        finalized_asset_count=0,
        source_cursor=0,
        source_exhausted=False,
        library_generation=3,
    )


def test_start_creates_an_exact_user_defined_generation() -> None:
    value = program()

    transition_library_program(
        value,
        action="start",
        expected_revision=4,
        selection_order="random",
        requested_asset_count=37,
        completion_batch_size=6,
        cohort_mode="custom",
    )

    assert value.state == LibraryProgramState.RUNNING
    assert value.requested_asset_count == 37
    assert value.original_requested_asset_count == 37
    assert value.selection_order == "random"
    assert value.completion_batch_size == 6
    assert value.cohort_mode == "custom"
    assert value.random_seed
    assert value.library_generation == 4
    assert value.admitted_asset_count == 0
    assert value.finalized_asset_count == 0
    assert value.source_exhausted is False


async def test_hard_stop_requeues_active_work_without_consuming_an_attempt(session) -> None:
    value = program()
    value.state = LibraryProgramState.RUNNING
    value.enabled = True
    session.add(value)
    await session.flush()
    active = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4()), "program_id": str(value.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="hard-stop-active-task",
        state=TaskState.RUNNING,
        attempt_count=1,
        lease_id=uuid.uuid4(),
        lease_expires_at=utc_now(),
        progress={"stage": "processing"},
        checkpoint={"partial": True},
    )
    session.add(active)
    await session.flush()

    interrupted = await hard_stop_library_work(
        session,
        program=value,
        expected_revision=4,
    )

    assert interrupted == 1
    assert value.state == LibraryProgramState.PAUSED
    assert value.enabled is True
    assert value.revision == 5
    assert active.state == TaskState.QUEUED
    assert active.attempt_count == 0
    assert active.lease_id is None
    assert active.lease_expires_at is None
    assert active.progress == {"stage": "queued", "reason": "operator_hard_stop"}
    assert active.checkpoint == {}


async def test_library_clear_never_cancels_independent_owner_work(session) -> None:
    value = program()
    value.state = LibraryProgramState.RUNNING
    value.enabled = True
    session.add(value)
    await session.flush()
    library_task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        target_ref={"program_id": str(value.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="clear-owned-library-task",
        state=TaskState.QUEUED,
    )
    manual_task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        target_ref={
            "program_id": str(value.id),
            "queue_origin": "user",
            "manual_request_id": str(uuid.uuid4()),
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="clear-independent-owner-task",
        state=TaskState.QUEUED,
    )
    series_task = Task(
        task_type="group.series_select",
        routing_key="medium-vlm",
        target_ref={"program_id": str(value.id)},
        requirements={"capabilities": ["medium-vlm"]},
        idempotency_key="clear-independent-series-task",
        state=TaskState.QUEUED,
    )
    session.add_all([library_task, manual_task, series_task])
    await session.flush()

    cancelled, running = await clear_library_queue(
        session,
        program=value,
        expected_revision=4,
    )

    assert (cancelled, running) == (1, 0)
    assert library_task.state == TaskState.CANCELLED
    assert manual_task.state == TaskState.QUEUED
    assert series_task.state == TaskState.QUEUED


def test_more_photographs_can_be_asked_for_while_the_run_is_working() -> None:
    """Asking for more used to mean stopping the run first, which is backwards.

    The owner should be able to add to what is already going: the photographs
    already admitted keep their place, the target grows, and admission looks
    at Immich again.
    """

    from star.db.models import LibraryProgram
    from star.domain.enums import LibraryProgramState
    from star.services.library_program import requested_run_count, transition_library_program

    program = LibraryProgram(
        name="adds",
        state=LibraryProgramState.RUNNING,
        enabled=True,
        revision=4,
        selection_order="random",
        work_scope="immich",
        ingest_new_assets=True,
        requested_asset_count=40_000,
        original_requested_asset_count=40_000,
        admitted_asset_count=40_000,
        finalized_asset_count=39_988,
        library_generation=7,
        run_started_at=utc_now(),
        source_exhausted=True,
    )

    transition_library_program(
        program, action="add", expected_revision=4, requested_asset_count=50_000
    )

    assert program.state == LibraryProgramState.RUNNING
    assert program.requested_asset_count == 90_000
    assert requested_run_count(program) == 90_000, "the screen shows the new total"
    assert program.admitted_asset_count == 40_000, "already admitted photographs keep their place"
    assert program.library_generation == 7, "it is the same run, not a new one"
    assert program.source_exhausted is False, "Immich is looked at again"
    requests = program.model_pipeline_policy["import_requests"]
    assert requests[-1]["photos"] == 50_000 and requests[-1]["run_total"] == 90_000


def test_adding_photographs_to_a_stopped_library_starts_a_run() -> None:
    """With nothing running, adding is simply how a run begins."""

    from star.db.models import LibraryProgram
    from star.domain.enums import LibraryProgramState
    from star.services.library_program import transition_library_program

    program = LibraryProgram(
        name="adds-fresh",
        state=LibraryProgramState.DISABLED,
        revision=2,
        library_generation=3,
        selection_order="newest",
    )

    transition_library_program(
        program, action="add", expected_revision=2, requested_asset_count=5_000
    )

    assert program.state == LibraryProgramState.RUNNING
    assert program.library_generation == 4 and program.run_started_at is not None
    assert program.requested_asset_count == 5_000
    assert program.admitted_asset_count == 0


def test_adding_everything_available_drops_the_number_entirely() -> None:
    from star.db.models import LibraryProgram
    from star.domain.enums import LibraryProgramState
    from star.services.library_program import transition_library_program

    program = LibraryProgram(
        name="adds-all",
        state=LibraryProgramState.PAUSED,
        revision=9,
        library_generation=2,
        requested_asset_count=1_000,
        original_requested_asset_count=1_000,
        run_started_at=utc_now(),
    )

    transition_library_program(program, action="add", expected_revision=9, all_assets=True)

    assert program.requested_asset_count is None
    assert program.state == LibraryProgramState.RUNNING
