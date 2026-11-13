"""Durable Apple work-block bookkeeping; queue-first, never time-first."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import MachineWorkBlock, Task, Worker
from star.domain.enums import TaskState


# The Apple worker has one phase since v209 (review); the owner order of work was
# retired in v255 in favour of the automatic model turn.
# "Ministral first") keeps the six Ministral lanes busy while the review queue
# has work; ``senior`` ("Advanced first") leases Advanced reviews before
# primary help; ``balanced`` keeps primary help but lets ready Advanced work
# pre-empt it. Apple Vision was retired in v209, so the Apple worker has one phase.
def next_phase(block: MachineWorkBlock, *, review_ready: bool) -> tuple[str, str]:
    """The Apple worker accelerator has one phase since v209: review.

    The durable block row keeps its bookkeeping (active seconds, photos) for
    the Machines dashboard; the reason names why the lane is or is not busy.
    """

    del block
    return "review", ("review_priority" if review_ready else "review_queue_empty")


async def observe_block(
    session: AsyncSession, *, peers: list[Worker], now: datetime
) -> MachineWorkBlock:
    machine = str(peers[0].runtime_status["machine_group"])
    block = await session.get(MachineWorkBlock, machine, with_for_update=True)
    if block is None:
        block = MachineWorkBlock(machine_group=machine, observed_at=now)
        session.add(block)
        await session.flush()
    previous = (
        block.observed_at.replace(tzinfo=UTC)
        if block.observed_at.tzinfo is None
        else block.observed_at
    )
    elapsed = max(0.0, (now - previous).total_seconds())
    # Lost heartbeat intervals are unknown/offline, never booked as hours of work.
    if block.was_busy and elapsed <= 90:
        block.active_seconds += elapsed
    block.was_busy = bool(
        await session.scalar(
            select(
                exists().where(
                    Task.leased_to_worker_id.in_([peer.id for peer in peers]),
                    Task.routing_key.in_(("small-vlm", "medium-vlm")),
                    Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                    Task.lease_expires_at > now,
                )
            )
        )
    )
    block.observed_at = now
    return block


async def observe_existing_block(
    session: AsyncSession, worker: Worker, *, now: datetime
) -> MachineWorkBlock | None:
    machine = worker.runtime_status.get("machine_group")
    if not machine or await session.get(MachineWorkBlock, str(machine)) is None:
        return None
    peers = list(
        await session.scalars(
            select(Worker).where(
                Worker.revoked_at.is_(None),
                Worker.runtime_status["machine_group"].as_string() == str(machine),
            )
        )
    )
    if not peers:
        return None
    return await observe_block(session, peers=peers, now=now)


def transition_block(block: MachineWorkBlock, phase: str, reason: str) -> None:
    if phase == block.phase:
        return
    if block.phase == "review" and block.active_seconds > 0:
        block.target_photos = max(
            500, min(1000, round(block.completed_photos * 3600 / block.active_seconds))
        )
    block.block_id = uuid.uuid4()
    block.phase = phase
    block.reason = reason
    block.active_seconds = 0
    block.completed_photos = 0
    block.was_busy = False
