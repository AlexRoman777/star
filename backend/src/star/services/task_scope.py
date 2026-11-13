"""Shared validity rules for leasing, queue repair, and task producers.

One question, one answer, and a name for it: does this task still describe the
photograph it was made for? The name matters as much as the answer. A lease
that refuses "obsolete" thousands of times says nothing about why, and the
apple sat idle for days behind refusals no surface could explain.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import AnalysisGeneration, Asset, Task
from star.services.analysis_generations import revision_still_describes_the_same_photograph

MANUAL_ORIGINS = frozenset({"manual", "user", "experiment"})


async def task_currency_reason(session: AsyncSession, task: Task) -> str | None:
    """Why a task no longer applies to its photograph, or None while it still does."""

    target = task.target_ref if isinstance(task.target_ref, dict) else {}
    if task.asset_id is None:
        return None
    asset = await session.get(Asset, task.asset_id)
    if asset is None:
        return "asset_missing"
    if getattr(asset, "access_state", "available") != "available":
        return f"asset_{asset.access_state}"
    manual = target.get("queue_origin") in MANUAL_ORIGINS
    generation_id = getattr(task, "analysis_generation_id", None)
    if generation_id is not None:
        generation = await session.get(AnalysisGeneration, generation_id)
        if generation is None:
            return "generation_missing"
        if generation.asset_id != asset.id:
            return "generation_belongs_to_another_photo"
        if (
            generation.asset_revision_id != asset.current_revision_id
            and not await revision_still_describes_the_same_photograph(
                session, generation=generation, asset=asset
            )
        ):
            return "photo_changed_since_generation"
        if generation.state == "published":
            if asset.current_generation_id != generation.id:
                return "generation_published_but_not_current"
        elif generation.state != "building":
            return f"generation_{generation.state}"
        if not manual:
            newer = await session.scalar(
                select(AnalysisGeneration.id)
                .where(
                    AnalysisGeneration.asset_id == asset.id,
                    AnalysisGeneration.asset_revision_id == asset.current_revision_id,
                    AnalysisGeneration.state == "building",
                    AnalysisGeneration.id != generation.id,
                    or_(
                        AnalysisGeneration.sequence > generation.sequence,
                        AnalysisGeneration.asset_revision_id != generation.asset_revision_id,
                    ),
                )
                .limit(1)
            )
            if newer is not None:
                return "newer_generation_building"
        # The current published review remains eligible for its Advanced follow-up
        # across intake epochs. Its immutable generation outranks copied task counters.
        scope = generation.scope if isinstance(generation.scope, dict) else {}
        epoch = scope.get("library_generation")
        if (
            generation.state == "building"
            and not manual
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
            and epoch != asset.analysis_generation
        ):
            return "generation_from_older_intake"
        return None
    revision_id = getattr(task, "asset_revision_id", None)
    if revision_id is not None and revision_id != asset.current_revision_id:
        return "task_revision_superseded"
    if manual:
        return None
    epoch = target.get("library_generation")
    if (
        isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and asset.analysis_generation != epoch
    ):
        return "task_from_older_intake"
    return None


async def task_generation_is_current(session: AsyncSession, task: Task) -> bool:
    return await task_currency_reason(session, task) is None


__all__ = ["MANUAL_ORIGINS", "task_currency_reason", "task_generation_is_current"]
