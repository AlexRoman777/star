from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.grouping import _observations, _store_group
from star.db.models import (
    AnalysisRun,
    CleanupCampaign,
    VisualFingerprint,
)
from star.domain.enums import GroupType, MediaType


async def campaign_image_asset_ids(
    session: AsyncSession,
    campaign: CleanupCampaign,
) -> list[uuid.UUID]:
    observations = await _observations(session, campaign)
    return sorted(
        (item.asset.id for item in observations if item.asset.media_type == MediaType.IMAGE),
        key=str,
    )


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


@dataclass
class _HashCluster:
    representative: str
    members: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@dataclass
class _BKNode:
    value: str
    cluster: _HashCluster
    children: dict[int, _BKNode] = field(default_factory=dict)


class _BKTree:
    def __init__(self) -> None:
        self.root: _BKNode | None = None

    def add(self, value: str, cluster: _HashCluster) -> None:
        if self.root is None:
            self.root = _BKNode(value=value, cluster=cluster)
            return
        node = self.root
        while True:
            distance = _hamming(value, node.value)
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _BKNode(value=value, cluster=cluster)
                return
            node = child

    def nearest(self, value: str, threshold: int) -> tuple[_HashCluster, int] | None:
        if self.root is None:
            return None
        best: tuple[_HashCluster, int] | None = None
        pending = [self.root]
        while pending:
            node = pending.pop()
            distance = _hamming(value, node.value)
            if distance <= threshold and (best is None or distance < best[1]):
                best = (node.cluster, distance)
            lower = distance - threshold
            upper = distance + threshold
            pending.extend(child for edge, child in node.children.items() if lower <= edge <= upper)
        return best


async def discover_perceptual_groups(
    session: AsyncSession,
    campaign: CleanupCampaign,
    *,
    threshold: int = 6,
) -> dict[str, int]:
    observations = await _observations(session, campaign)
    by_asset = {item.asset.id: item for item in observations}
    current_revisions = {item.asset.id: item.revision.id for item in observations}
    fingerprints = list(
        (
            await session.execute(
                select(
                    VisualFingerprint.asset_id,
                    VisualFingerprint.asset_revision_id,
                    VisualFingerprint.value,
                )
                .join(AnalysisRun, AnalysisRun.id == VisualFingerprint.analysis_run_id)
                .where(
                    VisualFingerprint.asset_id.in_(by_asset),
                    VisualFingerprint.algorithm == "phash",
                    VisualFingerprint.algorithm_version == "imagehash-4.3.2-hash8",
                    AnalysisRun.campaign_id == campaign.id,
                    AnalysisRun.status == "succeeded",
                )
            )
        ).all()
    )
    clusters: list[_HashCluster] = []
    tree = _BKTree()
    current_fingerprints = [
        (asset_id, value)
        for asset_id, revision_id, value in fingerprints
        if current_revisions.get(asset_id) == revision_id
    ]
    for asset_id, value in sorted(current_fingerprints, key=lambda item: item[1]):
        nearest = tree.nearest(value, threshold)
        if nearest is not None:
            nearest[0].members.append((asset_id, value))
        else:
            cluster = _HashCluster(representative=value, members=[(asset_id, value)])
            clusters.append(cluster)
            tree.add(value, cluster)
    counts = {"recompressed": 0, "same_scene": 0}
    for cluster in clusters:
        if len(cluster.members) < 2:
            continue
        unique_hashes = {value for _, value in cluster.members}
        group_type = GroupType.RECOMPRESSED if len(unique_hashes) == 1 else GroupType.SAME_SCENE
        confidence = 0.9 if group_type == GroupType.RECOMPRESSED else 0.68
        members = [by_asset[asset_id] for asset_id, _ in cluster.members if asset_id in by_asset]
        identity = min(unique_hashes) + ":" + str(len(cluster.members))
        if await _store_group(
            session,
            campaign=campaign,
            group_type=group_type,
            identity=identity,
            observations=members,
            confidence=confidence,
            explanation=(
                "Preview fingerprints are identical but file checksums differ; review as a "
                "probable recompressed or rendered copy."
                if group_type == GroupType.RECOMPRESSED
                else "Preview fingerprints are close enough to suggest the same visual scene. "
                "This is a similarity group, not a duplicate claim."
            ),
            estimate_cleanup=False,
        ):
            counts[group_type.value] += 1
    await session.flush()
    return counts
