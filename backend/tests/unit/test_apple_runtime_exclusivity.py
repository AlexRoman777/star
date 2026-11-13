"""A review machine runs one model runtime at a time: primary lanes or the Advanced model."""

from star.api.routers.workers.leasing import (
    _apple_review_block,
    _apple_runtime_conflicts,
)
from star.db.base import utc_now
from star.db.models import MachineWorkBlock, Task, Worker
from star.domain.enums import TaskState


def _helper_worker() -> Worker:
    return Worker(
        token_hash=("apple-deep-review" + "0" * 64)[:64],
        hostname_label="apple-deep-review",
        architecture="arm64",
        operating_system="macOS",
        software_version="test",
        capabilities={"apple-silicon": True, "small-vlm": True, "medium-vlm": True},
        allowed_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
        runtime_status={
            "machine_group": "apple-main",
            "resident_model_keys": [],
            "available_models": ["ministral-3:8b-instruct-2512-q4_K_M", "senior"],
            "model_idle_ttl_seconds": 30,
        },
        last_heartbeat=utc_now(),
    )


def _task(*, route: str, key: str, state: TaskState = TaskState.QUEUED) -> Task:
    model_key = "ministral-3:8b-instruct-2512-q4_K_M" if route == "small-vlm" else "senior"
    return Task(
        task_type="asset.primary_review" if route == "small-vlm" else "asset.deep_review",
        routing_key=route,
        data_class="family-preview",
        target_ref={"model_key": model_key},
        requirements={"capabilities": [route]},
        idempotency_key=key,
        state=state,
        available_at=utc_now(),
    )


async def test_primary_lanes_wait_while_the_machine_switches_to_advanced(
    session, test_settings
) -> None:
    """The automatic turn decided on the Advanced model: primary lanes stop taking work."""

    helper = _helper_worker()
    session.add(helper)
    session.add(_task(route="medium-vlm", key="senior-ready"))
    await session.flush()

    assert (
        await _apple_runtime_conflicts(
            session,
            worker=helper,
            routes={"small-vlm"},
            now=utc_now(),
            settings=test_settings,
            switching_to_senior=True,
        )
        == "senior_work_ready"
    )
    assert (
        await _apple_runtime_conflicts(
            session,
            worker=helper,
            routes={"small-vlm"},
            now=utc_now(),
            settings=test_settings,
            switching_to_senior=False,
        )
        is None
    )


async def test_running_primary_lane_drains_before_the_advanced_model_loads(
    session, test_settings
) -> None:
    helper = _helper_worker()
    session.add(helper)
    await session.flush()
    running = _task(route="small-vlm", key="primary-running", state=TaskState.RUNNING)
    running.leased_to_worker_id = helper.id
    running.lease_expires_at = utc_now().replace(year=2099)
    session.add(running)
    session.add(_task(route="medium-vlm", key="senior-ready"))
    await session.flush()

    assert (
        await _apple_runtime_conflicts(
            session,
            worker=helper,
            routes={"medium-vlm"},
            now=utc_now(),
            settings=test_settings,
            switching_to_senior=True,
        )
        == "model_lane_draining_for_senior"
    )


async def test_a_paused_apple_books_no_review_work(session, test_settings) -> None:
    helper = _helper_worker()
    helper.desired_state = "paused"
    session.add(helper)
    session.add(_task(route="small-vlm", key="primary-ready"))
    await session.flush()
    await _apple_review_block(session, peers=[helper], now=utc_now(), settings=test_settings)
    assert await session.get(MachineWorkBlock, "apple-main") is None
