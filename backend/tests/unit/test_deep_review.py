from __future__ import annotations

from star.db.models import (
    LibraryProgram,
    Task,
    User,
)
from star.domain.enums import (
    LibraryProgramState,
    Role,
    TaskState,
)
from star.services.deep_review import (
    ensure_active_policy_backfill,
    reasons_from_proposal,
)


def test_deep_review_threshold_and_exceptions_are_independent() -> None:
    strict = {
        "low_confidence": False,
        "technical_disagreement": False,
        "privacy_ambiguity": False,
        "recoverable_two_star": False,
        "ordinary_three_star": False,
        "protected_one_star": False,
    }
    assert (
        reasons_from_proposal(
            {"rating": 3},
            minimum_primary_rating=4,
            exceptions=strict,
        )
        == []
    )
    assert reasons_from_proposal(
        {"rating": 3},
        minimum_primary_rating=4,
        exceptions=strict,
        sampled_three_star=True,
    ) == ["three_star_audit_sample"]
    assert (
        reasons_from_proposal(
            {"rating": 2, "recovery_confidence": None, "visible_people_count": None},
            minimum_primary_rating=4,
            exceptions=strict,
        )
        == []
    )


async def test_a_settled_batch_registers_its_own_advanced_coverage(session) -> None:
    """The library must keep working after a batch finishes.

    Every photograph rated four or better before the Advanced review became the
    five-star authority is still waiting for its second look. The helper that
    registers that pass existed but nothing called it, and a finite job
    disqualified itself, so it had never run once. It is gated on work being in
    flight rather than on every photograph settling, because a dozen stuck
    photographs must not block the library for good.
    """

    owner = User(
        username="owner",
        display_name="Owner",
        role=Role.ADMINISTRATOR,
        is_active=True,
    )
    program = LibraryProgram(
        name="finite batch",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        requested_asset_count=100,
        original_requested_asset_count=100,
        admitted_asset_count=100,
        # Deliberately short: these photographs will never settle.
        finalized_asset_count=88,
    )
    session.add_all([owner, program])
    await session.flush()

    working = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="still-working",
        state=TaskState.QUEUED,
        target_ref={},
        requirements={},
    )
    session.add(working)
    await session.flush()
    # Work in flight: the coverage pass waits rather than competing with it.
    assert await ensure_active_policy_backfill(session, program=program) is None

    working.state = TaskState.SUCCEEDED
    await session.flush()
    # Nothing left in flight, and still twelve photographs short of settled:
    # this is exactly when the next pass should be registered.
    registered = await ensure_active_policy_backfill(session, program=program)
    assert registered is not None
    assert registered.state in {"running", "draining"}

    # Asking again does not stack a second pass on the same contract.
    assert await ensure_active_policy_backfill(session, program=program) is registered
