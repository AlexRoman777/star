from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    CampaignPolicyVersion,
    CleanupCampaign,
    HumanDecision,
    SimilarityGroup,
    SimilarityGroupMember,
    Task,
)
from star.domain.enums import (
    CampaignState,
    DecisionType,
    TaskState,
)
from star.domain.schemas import CampaignCreate, DecisionCreate


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def create_campaign(
    session: AsyncSession,
    *,
    payload: CampaignCreate,
    actor_id: uuid.UUID,
) -> CleanupCampaign:
    policy_data = {
        "connections": sorted(str(item) for item in payload.connection_ids),
        "scope": payload.scope,
        "candidate_breadth": payload.candidate_breadth,
        "raw_policy": payload.raw_policy,
        "automatic_tags": payload.automatic_tags,
        "rating_cleanup_independent": True,
    }
    campaign = CleanupCampaign(
        name=payload.name,
        objective=payload.objective,
        scope={"connection_ids": [str(item) for item in payload.connection_ids], **payload.scope},
        created_by=actor_id,
    )
    session.add(campaign)
    await session.flush()
    policy = CampaignPolicyVersion(
        campaign_id=campaign.id,
        version=1,
        policy_digest=canonical_digest(policy_data),
        rules=policy_data,
        candidate_breadth=payload.candidate_breadth,
        raw_policy=payload.raw_policy,
        automatic_tag_policy=payload.automatic_tags,
    )
    session.add(policy)
    await session.flush()
    campaign.active_policy_version_id = policy.id
    return campaign


async def start_campaign(session: AsyncSession, campaign: CleanupCampaign) -> Task:
    task_key = canonical_digest(
        {
            "type": "campaign.analyze",
            "campaign_id": str(campaign.id),
            "policy_id": str(campaign.active_policy_version_id),
        }
    )
    existing = await session.scalar(select(Task).where(Task.idempotency_key == task_key))
    if existing is not None and campaign.state == CampaignState.ANALYZING:
        return existing
    if campaign.state != CampaignState.DRAFT:
        raise ValueError("only draft campaigns can be started")
    campaign.state = CampaignState.ANALYZING
    campaign.started_at = utc_now()
    if existing is not None:
        return existing
    task = Task(
        campaign_id=campaign.id,
        task_type="campaign.analyze",
        routing_key="metadata",
        data_class="metadata-only",
        estimated_work_class="short",
        target_ref={"campaign_id": str(campaign.id)},
        requirements={"capabilities": ["metadata", "image-quality"], "sensitivity": "family-media"},
        idempotency_key=task_key,
        state=TaskState.PLANNED,
        priority=20,
    )
    session.add(task)
    await session.flush()
    return task


async def add_group_decision(
    session: AsyncSession,
    *,
    group: SimilarityGroup,
    actor_id: uuid.UUID,
    payload: DecisionCreate,
) -> HumanDecision:
    existing = await session.scalar(
        select(HumanDecision).where(HumanDecision.idempotency_key == payload.idempotency_key)
    )
    if existing:
        if (
            existing.decision_type != payload.action
            or existing.value != payload.value
            or existing.expected_revision != payload.expected_revision
        ):
            raise RuntimeError("idempotency key was already used for a different decision")
        return existing
    if payload.expected_revision != group.group_version:
        raise RuntimeError("review item changed; reload before deciding")
    asset_actions = {
        DecisionType.RATE,
        DecisionType.KEEP,
        DecisionType.PROTECT,
        DecisionType.CLEANUP_CANDIDATE,
        DecisionType.BEST_OUTPUT,
        DecisionType.EXCLUDE,
    }
    if payload.action in asset_actions:
        asset_id_value = payload.value.get("asset_id")
        if not asset_id_value:
            raise ValueError(f"{payload.action.value} decisions require asset_id")
        asset_id = uuid.UUID(str(asset_id_value))
        member = await session.scalar(
            select(SimilarityGroupMember).where(
                SimilarityGroupMember.group_id == group.id,
                SimilarityGroupMember.asset_id == asset_id,
            )
        )
        if member is None:
            raise ValueError("asset is not a member of this group")
        if payload.action == DecisionType.CLEANUP_CANDIDATE and member.is_protected:
            raise PermissionError("protected assets cannot enter the normal cleanup-candidate path")
    supersedes_id: uuid.UUID | None = None
    if payload.action == DecisionType.UNDO:
        decision_id_value = payload.value.get("decision_id")
        if not decision_id_value:
            raise ValueError("undo decisions require decision_id")
        try:
            target_decision_id = uuid.UUID(str(decision_id_value))
        except ValueError as exc:
            raise ValueError("undo decision_id is invalid") from exc
        target_decision = await session.get(HumanDecision, target_decision_id)
        if (
            target_decision is None
            or target_decision.target_type != "similarity_group"
            or target_decision.target_id != group.id
        ):
            raise ValueError("undo target is outside this review group")
        if target_decision.actor_id != actor_id:
            raise PermissionError("reviewers can undo only their own decision")
        if target_decision.decision_type == DecisionType.UNDO:
            raise ValueError("an undo decision cannot itself be an undo target")
        already_superseded = await session.scalar(
            select(HumanDecision.id).where(HumanDecision.supersedes_id == target_decision.id)
        )
        if already_superseded is not None:
            raise RuntimeError("the selected decision is no longer current")
        supersedes_id = target_decision.id
    else:
        asset_dimension = str(payload.value.get("asset_id") or "")
        candidates = list(
            (
                await session.scalars(
                    select(HumanDecision)
                    .where(
                        HumanDecision.target_type == "similarity_group",
                        HumanDecision.target_id == group.id,
                        HumanDecision.decision_type == payload.action,
                    )
                    .order_by(HumanDecision.created_at.desc())
                )
            ).all()
        )
        latest: HumanDecision | None = None
        for candidate in candidates:
            if str(candidate.value.get("asset_id") or "") != asset_dimension:
                continue
            superseding_id = await session.scalar(
                select(HumanDecision.id).where(HumanDecision.supersedes_id == candidate.id)
            )
            if superseding_id is None:
                latest = candidate
                break
        supersedes_id = latest.id if latest else None
    decision = HumanDecision(
        actor_id=actor_id,
        campaign_id=group.campaign_id,
        target_type="similarity_group",
        target_id=group.id,
        decision_type=payload.action,
        value=payload.value,
        expected_revision=payload.expected_revision,
        supersedes_id=supersedes_id,
        idempotency_key=payload.idempotency_key,
    )
    session.add(decision)
    group.group_version += 1
    await session.flush()
    return decision


async def campaign_summary(session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
    group_count = await session.scalar(
        select(func.count())
        .select_from(SimilarityGroup)
        .where(SimilarityGroup.campaign_id == campaign_id)
    )
    decision_count = await session.scalar(
        select(func.count())
        .select_from(HumanDecision)
        .where(HumanDecision.campaign_id == campaign_id)
    )
    return {"groups": int(group_count or 0), "decisions": int(decision_count or 0)}


async def child_task_status(session: AsyncSession, parent_task_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Task.state, func.count())
            .where(Task.parent_task_id == parent_task_id)
            .group_by(Task.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in rows}
    terminal = (
        counts.get(TaskState.SUCCEEDED.value, 0)
        + counts.get(TaskState.FAILED.value, 0)
        + counts.get(TaskState.CANCELLED.value, 0)
    )
    total = sum(counts.values())
    return {"total": total, "terminal": terminal, **counts}
