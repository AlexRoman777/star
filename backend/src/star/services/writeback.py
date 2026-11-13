from __future__ import annotations

import asyncio
import logging
import math
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.crypto import SecretCipher, constant_time_equal
from star.db.base import utc_now
from star.db.compat import ensure_writeback_batches_schema
from star.db.models import (
    AnalysisGeneration,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    HumanDecision,
    ImmichConnection,
    ImmichInstance,
    LibraryProgram,
    Session,
    WritebackBatch,
    WritebackOperation,
)
from star.domain.enums import CredentialKind, DecisionType, MediaType, WritebackState
from star.domain.schemas.library import (
    DEFAULT_WRITEBACK_MINIMUM_CONFIDENCE,
    WRITEBACK_FIELDS,
    WritebackPolicy,
    WritebackPolicyUpdate,
)
from star.domain.schemas.submissions import STAR_TAGS
from star.immich.client import ImmichApiError, ImmichClient
from star.services.audit import AuditRecord, append_audit_event
from star.services.campaigns import canonical_digest
from star.services.connections import get_active_credential
from star.services.operations import OperationRecord, append_operation_event

ALLOWED_WRITEBACK_OPERATIONS = frozenset({"rating", "tags", "description"})
PENDING_WRITEBACK_OPERATION_STATES = (
    WritebackState.PREVIEWED,
    WritebackState.APPROVED,
)
WRITEBACK_POLICY_VERSION = "rating-writeback-sample-v1"
CONTINUOUS_WRITEBACK_POLICY_VERSION = "selective-metadata-v61"
CONTINUOUS_WRITEBACK_CHUNK_SIZE = 500
WRITEBACK_EXECUTION_CHUNK_SIZE = 25
# Fallbacks for a library without a stored write-back policy. The owner sets the
# effective confidence threshold and field set per program (``WritebackPolicy``
# in ``LibraryProgram.model_pipeline_policy["writeback"]``).
MINIMUM_WRITEBACK_CONFIDENCE = DEFAULT_WRITEBACK_MINIMUM_CONFIDENCE
DEFAULT_WRITEBACK_WRITE_FIELDS: tuple[str, ...] = WRITEBACK_FIELDS
WRITEBACK_POLICY_KEY = "writeback"
MINIMUM_WRITEBACK_AGREEMENT = 1.0
MINIMUM_WRITEBACK_VOTE_COUNT = 1
REQUIRED_WRITEBACK_REVIEW_CONTRACT = "lean-review-v4"
CONTINUOUS_POLICY = {
    "minimum_confidence": MINIMUM_WRITEBACK_CONFIDENCE,
    "write_fields": list(DEFAULT_WRITEBACK_WRITE_FIELDS),
    "minimum_agreement": MINIMUM_WRITEBACK_AGREEMENT,
    "minimum_vote_count": MINIMUM_WRITEBACK_VOTE_COUNT,
    "required_review_contract": REQUIRED_WRITEBACK_REVIEW_CONTRACT,
    "protect_existing_nonzero_rating": True,
    "allowed_rating_sources": [
        "collection_relative_highlight",
        "star_human",
        "promoted_optional_review",
        "model_consensus",
    ],
}
# Owner-only tags: never proposed by a model, only set by the owner in Star.
OWNER_TAGS = ("edit", "print", "private", "sensitive")
# The exportable vocabulary is the model vocabulary (``StarTag``) plus the
# owner tags, each written to Immich as ``star/<tag>``.
CONTROLLED_TAGS: tuple[str, ...] = (*STAR_TAGS, *OWNER_TAGS)
PRIVATE_TAGS = frozenset({"private", "sensitive"})
MAX_CONTENT_TAG_EXPORTS = 5
MAX_TAG_EXPORTS = 16
PRIVATE_TAG_EXPORTS = {
    "star:nsfw": "star/private/sensitive",
    "star:nsfw:suggestive": "star/private/suggestive",
    "star:nsfw:sexually-explicit": "star/private/sexually-explicit",
    "star:nsfw:intimate": "star/private/intimate",
    "star:nsfw:nudity": "star/private/nudity",
    "star:nsfw:sexual-activity": "star/private/sexual-activity",
    "star:nsfw:erotic": "star/private/erotic",
    "star:nsfw:boudoir": "star/private/boudoir",
    "star:nsfw:review-needed": "star/private/review-needed",
    "star:nsfw:owner-marked": "star/private/owner-marked",
    "star:privacy:suggestive": "star/private/suggestive",
    "star:privacy:partial-nudity": "star/private/partial-nudity",
    "star:privacy:exposed-breast": "star/private/exposed-breast",
    "star:privacy:nudity": "star/private/nudity",
    "star:privacy:visible-genitals": "star/private/visible-genitals",
    "star:privacy:sexual-activity": "star/private/sexual-activity",
    "star:privacy:sexual-or-erotic": "star/private/sexual-or-erotic",
    "star:privacy:erotic": "star/private/erotic",
    "star:privacy:intimate-nonsexual": "star/private/intimate-nonsexual",
    "star:privacy:medical-private": "star/private/medical",
    "star:privacy:review-needed": "star/private/review-needed",
}
# Every controlled tag maps to itself; the aliases keep older free-text
# snapshots (before the vocabulary was enforced in the schema) exportable.
TAG_ALIASES: dict[str, str] = {
    **{tag: tag for tag in CONTROLLED_TAGS},
    "family": "family-moment",
    "family moment": "family-moment",
    "children": "child",
    "kid": "child",
    "kids": "child",
    "infant": "baby",
    "toddler": "baby",
    "dog": "pet",
    "cat": "pet",
    "city": "cityscape",
    "building": "architecture",
    "party": "celebration",
    "birthday": "celebration",
    "wedding": "celebration",
    "christmas": "holiday",
    "car": "vehicle",
    "sports": "sport",
    "sunrise": "sunset",
    "painting": "artwork",
    "house": "home",
    "office": "work",
}

logger = logging.getLogger(__name__)


async def _fetch_next_writeback_batch_with_fallback(
    session: AsyncSession,
    query: Any,
) -> WritebackBatch | None:
    try:
        return cast(WritebackBatch | None, await session.scalar(query))
    except ProgrammingError as exc:
        if "blocked_reasons" not in str(exc):
            raise
        logger.warning(
            "writeback_batches.blocked_reasons is missing; applying compatibility migration",
        )
        await session.rollback()
        await ensure_writeback_batches_schema()
        return cast(WritebackBatch | None, await session.scalar(query))


def _required_inspection_count(operation_count: int) -> int:
    if operation_count <= 30:
        return operation_count
    return min(100, max(30, math.ceil(operation_count * 0.05)))


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _initial_materialization_digest(selection: dict[str, Any], policy: WritebackPolicy) -> str:
    return canonical_digest(
        {
            "policy_version": CONTINUOUS_WRITEBACK_POLICY_VERSION,
            "policy": continuous_policy_for(policy),
            "selection": selection,
        }
    )


def _chain_digest(previous: str, value: dict[str, Any]) -> str:
    return canonical_digest({"previous": previous, "operation": value})


def effective_writeback_policy(program: LibraryProgram | None) -> WritebackPolicy:
    """The owner's write-back gate, or the released defaults when none is stored.

    A broken stored document falls back to the defaults rather than blocking
    every rating: the gate must stay explainable, never silently stricter.
    """

    if program is None:
        return WritebackPolicy.defaults()
    stored = (program.model_pipeline_policy or {}).get(WRITEBACK_POLICY_KEY)
    if not isinstance(stored, dict):
        return WritebackPolicy.defaults()
    try:
        return WritebackPolicy.model_validate(stored)
    except ValueError:
        logger.warning("stored write-back policy is invalid; using the defaults")
        return WritebackPolicy.defaults()


def writeback_policy_stored(program: LibraryProgram | None) -> bool:
    return program is not None and isinstance(
        (program.model_pipeline_policy or {}).get(WRITEBACK_POLICY_KEY), dict
    )


def update_writeback_policy(
    program: LibraryProgram, payload: WritebackPolicyUpdate
) -> tuple[WritebackPolicy, WritebackPolicy]:
    """Replace the stored write-back policy under the program's optimistic revision.

    Returns the (before, after) effective policies so the caller can audit
    the change.
    """

    if program.revision != payload.expected_revision:
        raise RuntimeError("library program changed; reload before updating it")
    before = effective_writeback_policy(program)
    program.model_pipeline_policy = {
        **(program.model_pipeline_policy or {}),
        WRITEBACK_POLICY_KEY: payload.policy.model_dump(mode="json"),
    }
    program.revision += 1
    program.updated_at = utc_now()
    return before, payload.policy


async def load_writeback_policy(session: AsyncSession) -> WritebackPolicy:
    """Load the library program once and return its effective write-back policy."""

    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    return effective_writeback_policy(program)


def continuous_policy_for(policy: WritebackPolicy) -> dict[str, Any]:
    """The eligibility policy a batch is materialized under, owner values applied."""

    return {
        **CONTINUOUS_POLICY,
        "minimum_confidence": policy.minimum_confidence,
        "write_fields": list(policy.write_fields),
    }


def batch_writeback_policy(batch: WritebackBatch) -> WritebackPolicy:
    """The policy frozen into a prepared batch (older batches carry the defaults)."""

    frozen = (batch.selection or {}).get("policy") if batch.selection else None
    if isinstance(frozen, dict):
        try:
            return WritebackPolicy.model_validate(frozen)
        except ValueError:
            pass
    return WritebackPolicy.defaults()


def _selection_conditions(selection: dict[str, Any]) -> list[Any]:
    mode = str(selection.get("mode") or "")
    filter_values = selection.get("filter")
    filters = filter_values if isinstance(filter_values, dict) else {}
    effective_rating = func.coalesce(
        AssetCurationSnapshot.effective_rating,
        AssetRevision.rating,
        0,
    )
    conditions: list[Any] = [
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        AssetRevision.is_trashed.is_(False),
    ]
    if mode == "assets":
        asset_ids = [uuid.UUID(str(value)) for value in selection.get("asset_ids", [])]
        conditions.append(Asset.id.in_(asset_ids))
    elif mode == "five_star":
        conditions.append(effective_rating == 5)
    elif mode not in {"filter", "all_eligible"}:
        raise ValueError("unsupported write-back selection mode")
    excluded_asset_ids = [
        uuid.UUID(str(value)) for value in selection.get("excluded_asset_ids", [])
    ]
    if excluded_asset_ids:
        conditions.append(Asset.id.not_in(excluded_asset_ids))

    connection_id = filters.get("connection_id")
    if connection_id:
        conditions.append(Asset.connection_id == uuid.UUID(str(connection_id)))
    query = filters.get("query")
    if isinstance(query, str) and query.strip():
        search = query.strip()
        filename = func.coalesce(
            AssetRevision.normalized_metadata["originalFileName"].as_string(), ""
        )
        camera_make = func.coalesce(
            AssetRevision.normalized_metadata["exifInfo"]["make"].as_string(), ""
        )
        camera_model = func.coalesce(
            AssetRevision.normalized_metadata["exifInfo"]["model"].as_string(), ""
        )
        conditions.append(
            or_(
                filename.icontains(search, autoescape=True),
                camera_make.icontains(search, autoescape=True),
                camera_model.icontains(search, autoescape=True),
                AssetCurationSnapshot.description.icontains(search, autoescape=True),
            )
        )
    rating_min = filters.get("rating_min")
    if filters.get("ratings"):
        conditions.append(effective_rating.in_(filters["ratings"]))
    rating_max = filters.get("rating_max")
    if isinstance(rating_min, int):
        conditions.append(effective_rating >= rating_min)
    if isinstance(rating_max, int):
        conditions.append(effective_rating <= rating_max)
    agreement_max = filters.get("agreement_max")
    if isinstance(agreement_max, (int, float)):
        conditions.append(func.coalesce(AssetCurationSnapshot.agreement, 0) <= agreement_max)
    vote_count_min = filters.get("vote_count_min")
    if isinstance(vote_count_min, int):
        conditions.append(func.coalesce(AssetCurationSnapshot.vote_count, 0) >= vote_count_min)
    analysis_state = filters.get("analysis_state", "all")
    has_analysis = exists(
        select(AnalysisRun.id).where(
            AnalysisRun.asset_id == Asset.id,
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
        )
    )
    if analysis_state == "analyzed":
        conditions.append(has_analysis)
    elif analysis_state == "waiting":
        conditions.append(~has_analysis)
    return conditions


async def create_continuous_rating_batch(
    session: AsyncSession,
    *,
    selection: dict[str, Any],
    actor_id: uuid.UUID,
    write_fields: set[str] | None = None,
    excluded_asset_ids: list[uuid.UUID] | None = None,
    excluded_tags: list[str] | None = None,
    include_private_tags: bool | None = None,
    policy: WritebackPolicy | None = None,
) -> WritebackBatch:
    # Validate before persisting. The Pydantic boundary performs shape/range validation;
    # this guard also protects non-HTTP callers.
    if policy is None:
        policy = await load_writeback_policy(session)
    allowed_fields = set(policy.write_fields)
    write_fields = set(allowed_fields) if write_fields is None else write_fields
    excluded_asset_ids = excluded_asset_ids or []
    excluded_tags = excluded_tags or []
    if not write_fields or not write_fields <= ALLOWED_WRITEBACK_OPERATIONS:
        raise ValueError("select at least one supported write-back field")
    if not write_fields <= allowed_fields:
        blocked = ", ".join(sorted(write_fields - allowed_fields))
        raise ValueError(f"the write-back policy does not allow writing {blocked}")
    if not set(excluded_tags) <= set(CONTROLLED_TAGS):
        raise ValueError("one or more excluded tags are unsupported")
    frozen_selection = {
        **selection,
        "write_fields": sorted(write_fields),
        "excluded_asset_ids": sorted(str(value) for value in set(excluded_asset_ids)),
        "excluded_tags": sorted(set(excluded_tags)),
        "include_private_tags": (
            policy.include_private_tags if include_private_tags is None else include_private_tags
        ),
        # The owner's gate is frozen with the selection so the preview digest
        # binds the threshold the cohort was materialized under.
        "policy": policy.model_dump(mode="json"),
    }
    _selection_conditions(frozen_selection)
    batch = WritebackBatch(
        created_by=actor_id,
        operation_class="selective_metadata",
        state=WritebackState.DRAFT,
        selection=frozen_selection,
        materialization_state="pending",
        materialization_digest_state=_initial_materialization_digest(frozen_selection, policy),
        policy_version=CONTINUOUS_WRITEBACK_POLICY_VERSION,
        required_inspection_count=0,
        completed_inspection_count=0,
        breakdown={"stars": {}, "accounts": [], "sources": {}, "blocked_reasons": {}},
        operation_counts={},
    )
    session.add(batch)
    await session.flush()
    return batch


def base_block_reason(
    asset: Asset,
    snapshot: AssetCurationSnapshot | None,
    generation: AnalysisGeneration | None,
) -> str | None:
    """Return the precise reason a photo cannot enter a write-back cohort.

    "Incomplete" used to cover every one of these causes at once, which made a
    missing star rating in Immich impossible to diagnose from the interface.
    """

    if snapshot is None:
        return "no_curation_snapshot"
    if snapshot.asset_revision_id != asset.current_revision_id:
        return "photo_changed_since_analysis"
    if not snapshot.evidence_digest:
        return "missing_evidence_digest"
    if snapshot.rating_source == "star_human":
        return None
    if generation is None or generation.id != asset.current_generation_id:
        return "no_published_generation"
    if generation.asset_revision_id != asset.current_revision_id:
        return "photo_changed_since_analysis"
    if generation.state != "published":
        return "final_review_not_complete"
    if snapshot.review_contract_version != REQUIRED_WRITEBACK_REVIEW_CONTRACT:
        return "analyzed_under_older_review_contract"
    return None


def _controlled_export_tags(
    snapshot: AssetCurationSnapshot,
    *,
    excluded: set[str],
    include_private: bool,
) -> list[str]:
    selected: list[str] = []
    for raw in snapshot.content_tags:
        normalized = " ".join(str(raw).lower().strip().replace("_", " ").split())
        mapped = TAG_ALIASES.get(normalized)
        if mapped is None:
            mapped = next(
                (
                    value
                    for key, value in TAG_ALIASES.items()
                    if len(key) >= 4 and key in normalized
                ),
                None,
            )
        if (
            mapped is None
            or mapped in excluded
            or (mapped in PRIVATE_TAGS and not include_private)
            or mapped in selected
        ):
            continue
        selected.append(mapped)
        if len(selected) == MAX_CONTENT_TAG_EXPORTS:
            break
    exported = [f"star/{tag}" for tag in selected]
    if include_private and not ({"private", "sensitive"} & excluded):
        for raw in snapshot.private_tags:
            mapped = PRIVATE_TAG_EXPORTS.get(str(raw))
            if mapped is not None and mapped not in exported:
                exported.append(mapped)
    return exported[:MAX_TAG_EXPORTS]


def _eligible_snapshot(
    asset: Asset,
    revision: AssetRevision,
    snapshot: AssetCurationSnapshot | None,
    generation: AnalysisGeneration | None,
    *,
    minimum_confidence: float = MINIMUM_WRITEBACK_CONFIDENCE,
) -> tuple[str, int | None]:
    if snapshot is None or snapshot.effective_rating is None:
        return "incomplete", None
    rating = snapshot.effective_rating
    if not 1 <= rating <= 5:
        return "incomplete", None
    if revision.rating not in {None, 0}:
        return ("already_current" if revision.rating == rating else "protected"), rating
    if snapshot.asset_revision_id != asset.current_revision_id or not snapshot.evidence_digest:
        return "stale", rating
    if snapshot.rating_source == "star_human":
        return "eligible", rating
    if snapshot.rating_source == "immich_human":
        return "rating_already_owned_by_immich", rating
    if snapshot.rating_source not in {
        "collection_relative_highlight",
        "model_consensus",
        "promoted_optional_review",
    }:
        return "no_automated_rating_yet", rating
    base = base_block_reason(asset, snapshot, generation)
    if base is not None:
        return base, rating
    if snapshot.vote_count < MINIMUM_WRITEBACK_VOTE_COUNT:
        return "no_recorded_model_vote", rating
    if snapshot.agreement < MINIMUM_WRITEBACK_AGREEMENT:
        return "model_votes_disagree", rating
    if snapshot.confidence < minimum_confidence:
        return "model_confidence_below_threshold", rating
    return "eligible", rating


RATING_OUTCOME_BUCKETS = {
    "eligible": "eligible",
    "protected": "protected",
    "already_current": "already_current",
    "stale": "stale",
    "photo_changed_since_analysis": "stale",
}


def rating_outcome_bucket(outcome: str) -> str:
    """Map a precise blocking reason onto the coarse batch counter it belongs to."""

    return RATING_OUTCOME_BUCKETS.get(outcome, "incomplete")


def _record_blocked_reason(batch: WritebackBatch, reason: str) -> None:
    reasons = dict(batch.blocked_reasons or {})
    reasons[reason] = int(reasons.get(reason, 0)) + 1
    batch.blocked_reasons = reasons
    # The preview breakdown carries the same per-reason counts so one response
    # explains eligible, blocked-by-reason, and written photos together.
    batch.breakdown = {**dict(batch.breakdown or {}), "blocked_reasons": dict(reasons)}


def _increment_breakdown(
    breakdown: dict[str, Any],
    *,
    rating: int,
    source: str,
    connection_id: uuid.UUID,
    connection_label: str,
) -> dict[str, Any]:
    stars = dict(breakdown.get("stars") or {})
    sources = dict(breakdown.get("sources") or {})
    accounts = {
        str(item.get("connection_id")): dict(item)
        for item in breakdown.get("accounts", [])
        if isinstance(item, dict) and item.get("connection_id")
    }
    stars[str(rating)] = int(stars.get(str(rating), 0)) + 1
    sources[source] = int(sources.get(source, 0)) + 1
    account_key = str(connection_id)
    current = accounts.get(
        account_key,
        {"connection_id": account_key, "label": connection_label, "count": 0},
    )
    current["count"] = int(current.get("count", 0)) + 1
    accounts[account_key] = current
    return {
        "stars": stars,
        "sources": sources,
        "accounts": sorted(accounts.values(), key=lambda item: str(item["label"]).lower()),
        "blocked_reasons": dict(breakdown.get("blocked_reasons") or {}),
    }


async def materialize_next_writeback_batch(
    session: AsyncSession,
    *,
    limit: int = CONTINUOUS_WRITEBACK_CHUNK_SIZE,
) -> bool:
    if limit < 1:
        raise ValueError("write-back materialization limit must be positive")
    batch = await _fetch_next_writeback_batch_with_fallback(
        session,
        select(WritebackBatch)
        .where(
            WritebackBatch.state == WritebackState.DRAFT,
            WritebackBatch.policy_version == CONTINUOUS_WRITEBACK_POLICY_VERSION,
            WritebackBatch.materialization_state.in_(["pending", "running"]),
        )
        .order_by(WritebackBatch.created_at, WritebackBatch.id)
        .with_for_update(skip_locked=True)
        .limit(1),
    )
    if batch is None:
        return False
    if batch.cancel_requested_at is not None:
        batch.materialization_state = "cancelled"
        batch.state = WritebackState.CANCELLED
        return True
    try:
        conditions = _selection_conditions(batch.selection)
        # New arrivals must not silently extend a preview while it is built.
        conditions.append(Asset.created_at <= batch.created_at)
        photo_limit = batch.selection.get("limit")
        if isinstance(photo_limit, int):
            limit = min(limit, max(0, photo_limit - batch.materialized_count))
        if batch.materialization_state == "pending":
            batch.total_candidates = int(
                await session.scalar(
                    select(func.count(Asset.id))
                    .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                    .outerjoin(
                        AssetCurationSnapshot,
                        AssetCurationSnapshot.asset_id == Asset.id,
                    )
                    .where(*conditions)
                )
                or 0
            )
            batch.materialization_state = "running"
            if isinstance(photo_limit, int):
                batch.total_candidates = min(batch.total_candidates, photo_limit)

        cursor_conditions = list(conditions)
        if batch.materialization_cursor is not None:
            cursor_conditions.append(Asset.id > batch.materialization_cursor)
        rows = list(
            (
                await session.execute(
                    select(
                        Asset,
                        AssetRevision,
                        AssetCurationSnapshot,
                        ImmichConnection,
                        AnalysisGeneration,
                    )
                    .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                    .join(ImmichConnection, ImmichConnection.id == Asset.connection_id)
                    .outerjoin(
                        AssetCurationSnapshot,
                        AssetCurationSnapshot.asset_id == Asset.id,
                    )
                    .outerjoin(
                        AnalysisGeneration,
                        AnalysisGeneration.id == Asset.current_generation_id,
                    )
                    .where(*cursor_conditions)
                    .order_by(Asset.id)
                    .limit(limit)
                )
            ).all()
        )
        policy = batch_writeback_policy(batch)
        digest = batch.materialization_digest_state or _initial_materialization_digest(
            batch.selection, policy
        )
        write_fields = {str(value) for value in batch.selection.get("write_fields", ["rating"])}
        excluded_tags = {str(value) for value in batch.selection.get("excluded_tags", [])}
        include_private_tags = batch.selection.get("include_private_tags") is True
        for asset, revision, snapshot, connection, generation in rows:
            batch.materialized_count += 1
            batch.materialization_cursor = asset.id
            base_reason = base_block_reason(asset, snapshot, generation)
            if base_reason is not None:
                _record_blocked_reason(batch, base_reason)
                if base_reason == "photo_changed_since_analysis":
                    batch.stale_count += 1
                else:
                    batch.incomplete_count += 1
                continue
            assert snapshot is not None
            created_for_asset = 0
            precise_reason_recorded = False
            if "rating" in write_fields:
                outcome, rating = _eligible_snapshot(
                    asset,
                    revision,
                    snapshot,
                    generation,
                    minimum_confidence=policy.minimum_confidence,
                )
                bucket = rating_outcome_bucket(outcome)
                if outcome != "eligible":
                    _record_blocked_reason(batch, outcome)
                    precise_reason_recorded = True
                if bucket == "protected":
                    batch.protected_count += 1
                elif bucket == "already_current":
                    batch.already_current_count += 1
                elif bucket == "stale":
                    batch.stale_count += 1
                elif outcome == "eligible" and rating is not None:
                    identity = {
                        "asset_id": str(asset.id),
                        "connection_id": str(asset.connection_id),
                        "asset_revision_id": str(revision.id),
                        "snapshot_updated_at": snapshot.updated_at.isoformat(),
                        "evidence_digest": snapshot.evidence_digest,
                        "operation_type": "rating",
                        "rating": rating,
                        "rating_source": snapshot.rating_source,
                    }
                    session.add(
                        WritebackOperation(
                            batch_id=batch.id,
                            connection_id=asset.connection_id,
                            asset_id=asset.id,
                            asset_revision_id=revision.id,
                            snapshot_updated_at=snapshot.updated_at,
                            evidence_digest=snapshot.evidence_digest,
                            operation_type="rating",
                            before_state={
                                "rating": revision.rating,
                                "revision_id": str(revision.id),
                                "metadata_digest": revision.metadata_digest,
                            },
                            requested_state={
                                "rating": rating,
                                "rating_source": snapshot.rating_source,
                                "confidence": snapshot.confidence,
                                "agreement": snapshot.agreement,
                                "vote_count": snapshot.vote_count,
                            },
                            idempotency_key=canonical_digest(
                                {"batch_id": str(batch.id), **identity}
                            ),
                            state=WritebackState.PREVIEWED,
                        )
                    )
                    created_for_asset += 1
                    batch.breakdown = _increment_breakdown(
                        batch.breakdown,
                        rating=rating,
                        source=snapshot.rating_source,
                        connection_id=asset.connection_id,
                        connection_label=connection.display_label,
                    )
                    digest = _chain_digest(digest, identity)

            if "description" in write_fields:
                description = (snapshot.description or "").strip()
                if description:
                    local_description = revision.normalized_metadata.get("description")
                    exif_info = revision.normalized_metadata.get("exifInfo")
                    if (
                        not local_description
                        and isinstance(exif_info, dict)
                        and isinstance(exif_info.get("description"), str)
                    ):
                        local_description = exif_info.get("description")
                    identity = {
                        "asset_id": str(asset.id),
                        "connection_id": str(asset.connection_id),
                        "asset_revision_id": str(revision.id),
                        "snapshot_updated_at": snapshot.updated_at.isoformat(),
                        "evidence_digest": snapshot.evidence_digest,
                        "operation_type": "description",
                        "description": description,
                        "rating_source": snapshot.rating_source,
                    }
                    session.add(
                        WritebackOperation(
                            batch_id=batch.id,
                            connection_id=asset.connection_id,
                            asset_id=asset.id,
                            asset_revision_id=revision.id,
                            snapshot_updated_at=snapshot.updated_at,
                            evidence_digest=snapshot.evidence_digest,
                            operation_type="description",
                            before_state={
                                "description": local_description,
                                "revision_id": str(revision.id),
                                "metadata_digest": revision.metadata_digest,
                            },
                            requested_state={
                                "description": description,
                                "rating_source": snapshot.rating_source,
                            },
                            idempotency_key=canonical_digest(
                                {"batch_id": str(batch.id), **identity}
                            ),
                            state=WritebackState.PREVIEWED,
                        )
                    )
                    created_for_asset += 1
                    digest = _chain_digest(digest, identity)

            if "tags" in write_fields:
                tags = _controlled_export_tags(
                    snapshot,
                    excluded=excluded_tags,
                    include_private=include_private_tags,
                )
                if tags:
                    identity = {
                        "asset_id": str(asset.id),
                        "connection_id": str(asset.connection_id),
                        "asset_revision_id": str(revision.id),
                        "snapshot_updated_at": snapshot.updated_at.isoformat(),
                        "evidence_digest": snapshot.evidence_digest,
                        "operation_type": "tags",
                        "tags": tags,
                        "rating_source": snapshot.rating_source,
                    }
                    session.add(
                        WritebackOperation(
                            batch_id=batch.id,
                            connection_id=asset.connection_id,
                            asset_id=asset.id,
                            asset_revision_id=revision.id,
                            snapshot_updated_at=snapshot.updated_at,
                            evidence_digest=snapshot.evidence_digest,
                            operation_type="tags",
                            before_state={
                                "tags": [],
                                "revision_id": str(revision.id),
                                "metadata_digest": revision.metadata_digest,
                            },
                            requested_state={
                                "tags": tags,
                                "rating_source": snapshot.rating_source,
                            },
                            idempotency_key=canonical_digest(
                                {"batch_id": str(batch.id), **identity}
                            ),
                            state=WritebackState.PREVIEWED,
                        )
                    )
                    created_for_asset += 1
                    digest = _chain_digest(digest, identity)

            if created_for_asset == 0 and not (
                "rating" in write_fields and revision.rating not in {None, 0}
            ):
                # A photo already explained by a precise rating reason is not
                # counted twice; the generic reason covers tags/description-only
                # selections that produced nothing to write.
                if not precise_reason_recorded:
                    _record_blocked_reason(batch, "no_selected_field_produced_a_change")
                batch.incomplete_count += 1
            batch.eligible_count += created_for_asset
        batch.materialization_digest_state = digest
        if rows:
            batch.operation_counts = {"previewed": batch.eligible_count}
        if len(rows) < limit or (
            isinstance(photo_limit, int) and batch.materialized_count >= photo_limit
        ):
            digest = canonical_digest(
                {
                    "previous": digest,
                    "summary": {
                        "total_candidates": batch.total_candidates,
                        "eligible_count": batch.eligible_count,
                        "protected_count": batch.protected_count,
                        "already_current_count": batch.already_current_count,
                        "stale_count": batch.stale_count,
                        "incomplete_count": batch.incomplete_count,
                        "breakdown": batch.breakdown,
                    },
                }
            )
            batch.materialization_digest_state = digest
            batch.preview_digest = digest
            batch.cohort_digest = digest
            batch.materialization_state = "complete"
            batch.state = WritebackState.PREVIEWED
            await append_operation_event(
                session,
                OperationRecord(
                    source="writeback",
                    event_type="writeback.cohort.prepared",
                    message="An immutable Immich metadata cohort is ready for approval",
                    correlation_id=str(batch.id),
                    details={
                        "eligible": batch.eligible_count,
                        "protected": batch.protected_count,
                        "already_current": batch.already_current_count,
                        "stale": batch.stale_count,
                        "incomplete": batch.incomplete_count,
                        "blocked_reasons": dict(batch.blocked_reasons or {}),
                        "minimum_confidence": policy.minimum_confidence,
                    },
                ),
            )
        await session.flush()
        return True
    except (TypeError, ValueError) as exc:
        batch.materialization_state = "failed"
        batch.state = WritebackState.FAILED
        batch.error_message = str(exc)[:1000]
        await session.flush()
        return True


async def create_rating_preview(
    session: AsyncSession,
    *,
    campaign_id: uuid.UUID,
    decision_ids: list[uuid.UUID],
    actor_id: uuid.UUID,
) -> WritebackBatch:
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("decision IDs must be unique")
    decisions = list(
        (
            await session.scalars(select(HumanDecision).where(HumanDecision.id.in_(decision_ids)))
        ).all()
    )
    if len(decisions) != len(set(decision_ids)):
        raise ValueError("one or more decisions do not exist")
    batch = WritebackBatch(created_by=actor_id, state=WritebackState.DRAFT)
    session.add(batch)
    await session.flush()
    digest_rows: list[dict[str, Any]] = []
    ranked_operations: list[tuple[int, str, WritebackOperation, list[str]]] = []
    for decision in decisions:
        if decision.campaign_id != campaign_id:
            raise ValueError("decision is outside the selected campaign")
        if decision.decision_type != DecisionType.RATE:
            raise ValueError("version one write-back accepts rating decisions only")
        asset_id = uuid.UUID(str(decision.value.get("asset_id")))
        rating = decision.value.get("rating")
        if not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("approved rating must be an integer from 1 to 5")
        asset = await session.get(Asset, asset_id)
        revision = (
            await session.get(AssetRevision, asset.current_revision_id)
            if asset and asset.current_revision_id
            else None
        )
        if asset is None or revision is None or asset.access_state != "available":
            raise ValueError("rating target is unavailable")
        before = {"rating": revision.rating, "revision_id": str(revision.id)}
        requested = {"rating": rating, "decision_id": str(decision.id)}
        identity = {
            "asset_id": str(asset.id),
            "connection_id": str(asset.connection_id),
            "before": before,
            "requested": requested,
        }
        digest_rows.append(identity)
        operation = WritebackOperation(
            batch_id=batch.id,
            connection_id=asset.connection_id,
            asset_id=asset.id,
            operation_type="rating",
            before_state=before,
            requested_state=requested,
            idempotency_key=canonical_digest({"batch_id": str(batch.id), **identity}),
            state=WritebackState.PREVIEWED,
        )
        risk_reasons: list[str] = []
        if revision.rating not in {None, 0}:
            risk_reasons.append("changes_existing_nonzero_rating")
        if isinstance(revision.rating, int) and rating < revision.rating:
            risk_reasons.append("rating_decrease")
        if isinstance(revision.rating, int) and abs(rating - revision.rating) >= 2:
            risk_reasons.append("large_rating_change")
        operation.risk_reasons = risk_reasons
        session.add(operation)
        ranked_operations.append(
            (
                len(risk_reasons),
                canonical_digest(identity),
                operation,
                risk_reasons,
            )
        )
    cohort_digest = canonical_digest(sorted(digest_rows, key=lambda row: str(row["asset_id"])))
    risk_outlier_count = sum(1 for score, _, _, _ in ranked_operations if score > 0)
    if risk_outlier_count > 100:
        raise ValueError("write-back cohort has more than 100 risk outliers; split the cohort")
    required_inspections = max(
        _required_inspection_count(len(ranked_operations)),
        risk_outlier_count,
    )
    ranked_operations.sort(key=lambda item: (-item[0], item[1]))
    for _, _, operation, _ in ranked_operations[:required_inspections]:
        operation.inspection_required = True
    batch.preview_digest = cohort_digest
    batch.cohort_digest = cohort_digest
    batch.policy_version = WRITEBACK_POLICY_VERSION
    batch.required_inspection_count = required_inspections
    batch.completed_inspection_count = 0
    batch.state = WritebackState.PREVIEWED
    await session.flush()
    return batch


async def approve_batch(
    session: AsyncSession,
    *,
    batch: WritebackBatch,
    user_session: Session,
    preview_digest: str,
) -> None:
    if batch.state != WritebackState.PREVIEWED or not batch.preview_digest:
        raise RuntimeError("write-back batch is not awaiting approval")
    if not constant_time_equal(batch.preview_digest, preview_digest):
        raise RuntimeError("write-back preview changed; generate it again")
    if batch.selection and batch.eligible_count < 1:
        raise RuntimeError("write-back cohort has no eligible ratings")
    if not batch.selection and batch.completed_inspection_count < batch.required_inspection_count:
        raise RuntimeError("the required risk-based inspection sample is incomplete")
    reauthenticated_at = _aware(user_session.reauthenticated_at)
    if not batch.selection and (
        reauthenticated_at is None or reauthenticated_at < utc_now() - timedelta(minutes=15)
    ):
        raise PermissionError("a sign-in within the last 15 minutes is required")
    batch.state = WritebackState.APPROVED
    batch.approved_at = utc_now()
    batch.approval_session_id = user_session.id
    batch.expires_at = utc_now() + timedelta(minutes=10)


async def mark_operation_inspected(
    session: AsyncSession,
    *,
    batch: WritebackBatch,
    operation_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> WritebackOperation:
    if batch.state != WritebackState.PREVIEWED:
        raise RuntimeError("only a previewed batch can be inspected")
    operation = await session.get(WritebackOperation, operation_id)
    if operation is None or operation.batch_id != batch.id:
        raise ValueError("write-back operation is outside this batch")
    if not operation.inspection_required:
        raise ValueError("write-back operation is not part of the required sample")
    if operation.inspected_at is None:
        operation.inspected_by = actor_id
        operation.inspected_at = utc_now()
        batch.completed_inspection_count += 1
    await session.flush()
    return operation


async def batch_operations(session: AsyncSession, batch_id: uuid.UUID) -> list[WritebackOperation]:
    return list(
        (
            await session.scalars(
                select(WritebackOperation)
                .where(WritebackOperation.batch_id == batch_id)
                .order_by(WritebackOperation.id)
            )
        ).all()
    )


async def refresh_batch_operation_counts(
    session: AsyncSession,
    batch: WritebackBatch,
) -> dict[str, int]:
    rows = list(
        (
            await session.execute(
                select(WritebackOperation.state, func.count(WritebackOperation.id))
                .where(WritebackOperation.batch_id == batch.id)
                .group_by(WritebackOperation.state)
            )
        ).all()
    )
    counts = {
        state.value if isinstance(state, WritebackState) else str(state): int(count)
        for state, count in rows
    }
    batch.operation_counts = counts
    return counts


async def cancel_writeback_batch(session: AsyncSession, batch: WritebackBatch) -> None:
    if batch.state in {
        WritebackState.SUCCEEDED,
        WritebackState.CONFLICT,
        WritebackState.FAILED,
        WritebackState.CANCELLED,
    }:
        raise RuntimeError("completed write-back batch cannot be cancelled")
    batch.cancel_requested_at = utc_now()
    if batch.state != WritebackState.RUNNING:
        batch.state = WritebackState.CANCELLED
        batch.materialization_state = (
            "cancelled"
            if batch.materialization_state != "complete"
            else batch.materialization_state
        )
        await session.execute(
            update(WritebackOperation)
            .where(
                WritebackOperation.batch_id == batch.id,
                WritebackOperation.state.in_(PENDING_WRITEBACK_OPERATION_STATES),
            )
            .values(state=WritebackState.CANCELLED)
        )
        await refresh_batch_operation_counts(session, batch)
    await session.flush()


def _remote_rating_value(remote: dict[str, Any]) -> object:
    top_level = remote.get("rating")
    exif_rating = (
        remote["exifInfo"].get("rating") if isinstance(remote.get("exifInfo"), dict) else None
    )
    if top_level is not None and top_level != 0:
        return top_level
    if exif_rating is not None and exif_rating != 0:
        return exif_rating
    return top_level if top_level is not None else exif_rating


def _remote_rating(remote: dict[str, Any]) -> int | None:
    value = _remote_rating_value(remote)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        return None
    return value


def _snapshot_matches_operation(
    asset: Asset,
    snapshot: AssetCurationSnapshot | None,
    operation: WritebackOperation,
) -> bool:
    if not operation.evidence_digest:
        return True  # Legacy batches predate immutable curation snapshots.
    if snapshot is None:
        return False
    identity_matches = (
        asset.current_revision_id == operation.asset_revision_id
        and snapshot.asset_revision_id == operation.asset_revision_id
        and snapshot.evidence_digest == operation.evidence_digest
        and _aware(snapshot.updated_at) == _aware(operation.snapshot_updated_at)
        and snapshot.rating_source == operation.requested_state.get("rating_source")
    )
    if not identity_matches:
        return False
    if operation.operation_type == "rating":
        return snapshot.effective_rating == operation.requested_state.get("rating")
    if operation.operation_type == "description":
        return (snapshot.description or "").strip() == operation.requested_state.get("description")
    if operation.operation_type == "tags":
        return set(operation.requested_state.get("tags", [])) <= set(
            _controlled_export_tags(
                snapshot,
                excluded=set(),
                include_private=True,
            )
        )
    return False


def _remote_description(remote: dict[str, Any]) -> str | None:
    top_level = remote.get("description")
    exif_info = remote.get("exifInfo")
    exif_description = exif_info.get("description") if isinstance(exif_info, dict) else None
    value = top_level if isinstance(top_level, str) else exif_description
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _remote_tag_names(remote: dict[str, Any]) -> set[str]:
    raw_tags = remote.get("tags")
    if not isinstance(raw_tags, list):
        return set()
    return {
        str(item.get("value") or item.get("name"))
        for item in raw_tags
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


async def execute_next_approved_batch(
    session: AsyncSession,
    *,
    cipher: SecretCipher,
) -> bool:
    now = utc_now()
    batch = await _fetch_next_writeback_batch_with_fallback(
        session,
        select(WritebackBatch)
        .where(
            or_(
                WritebackBatch.state == WritebackState.APPROVED,
                and_(
                    WritebackBatch.state == WritebackState.RUNNING,
                    or_(
                        WritebackBatch.execution_lease_expires_at.is_(None),
                        WritebackBatch.execution_lease_expires_at < now,
                    ),
                ),
            )
        )
        .order_by(WritebackBatch.approved_at)
        .with_for_update(skip_locked=True)
        .limit(1),
    )
    if batch is None:
        return False
    expires_at = _aware(batch.expires_at)
    if batch.state == WritebackState.APPROVED and (expires_at is None or expires_at <= now):
        batch.state = WritebackState.CANCELLED
        await append_operation_event(
            session,
            OperationRecord(
                source="writeback",
                event_type="writeback.batch.expired",
                message="An approved Immich metadata batch expired before execution",
                severity="warning",
                correlation_id=str(batch.id),
                details={"state": batch.state.value},
            ),
        )
        await session.commit()
        return True
    is_starting = batch.state == WritebackState.APPROVED
    batch.state = WritebackState.RUNNING
    batch.execution_lease_id = uuid.uuid4()
    batch.execution_lease_expires_at = now + timedelta(minutes=5)
    if is_starting:
        await append_operation_event(
            session,
            OperationRecord(
                source="writeback",
                event_type="writeback.batch.started",
                message="An approved Immich metadata batch started",
                correlation_id=str(batch.id),
                details={"count": batch.eligible_count, "state": batch.state.value},
            ),
        )
    # Commit the short acquisition transaction so a crashed writer leaves a visible,
    # expiring lease instead of holding a database lock during Immich network calls.
    await session.commit()
    await session.refresh(batch, attribute_names=["cancel_requested_at"])

    if batch.cancel_requested_at is not None:
        operations = list(
            (
                await session.scalars(
                    select(WritebackOperation)
                    .where(
                        WritebackOperation.batch_id == batch.id,
                        WritebackOperation.state.in_(PENDING_WRITEBACK_OPERATION_STATES),
                    )
                    .order_by(WritebackOperation.id)
                    .limit(WRITEBACK_EXECUTION_CHUNK_SIZE)
                )
            ).all()
        )
        for operation in operations:
            operation.state = WritebackState.CANCELLED
        remaining = int(
            await session.scalar(
                select(func.count(WritebackOperation.id)).where(
                    WritebackOperation.batch_id == batch.id,
                    WritebackOperation.state.in_(PENDING_WRITEBACK_OPERATION_STATES),
                )
            )
            or 0
        )
        if remaining == 0:
            batch.state = WritebackState.CANCELLED
        batch.execution_lease_id = None
        batch.execution_lease_expires_at = None
        await refresh_batch_operation_counts(session, batch)
        await session.commit()
        return True

    operations = list(
        (
            await session.scalars(
                select(WritebackOperation)
                .where(
                    WritebackOperation.batch_id == batch.id,
                    WritebackOperation.state.in_(PENDING_WRITEBACK_OPERATION_STATES),
                )
                .order_by(WritebackOperation.id)
                .limit(WRITEBACK_EXECUTION_CHUNK_SIZE)
            )
        ).all()
    )
    retry_not_before: datetime | None = None
    if operations:
        async with AsyncExitStack() as stack:
            clients: dict[tuple[uuid.UUID, CredentialKind], ImmichClient] = {}
            for operation in operations:
                await session.commit()
                await session.refresh(batch, attribute_names=["cancel_requested_at"])
                if batch.cancel_requested_at is not None:
                    break
                asset = await session.get(Asset, operation.asset_id)
                connection = await session.get(ImmichConnection, operation.connection_id)
                instance = (
                    await session.get(ImmichInstance, connection.instance_id)
                    if connection is not None
                    else None
                )
                snapshot = (
                    await session.get(AssetCurationSnapshot, operation.asset_id)
                    if batch.selection
                    else None
                )
                current_revision = (
                    await session.get(AssetRevision, asset.current_revision_id)
                    if asset is not None and asset.current_revision_id is not None
                    else None
                )
                attempt_number = operation.attempt_count + 1
                event_suffix = "failed"
                if operation.operation_type not in ALLOWED_WRITEBACK_OPERATIONS:
                    operation.state = WritebackState.FAILED
                    operation.after_state = {"error": "operation_not_allowed"}
                    event_suffix = "rejected"
                elif (
                    asset is None
                    or connection is None
                    or instance is None
                    or (bool(batch.selection) and current_revision is None)
                ):
                    operation.state = WritebackState.FAILED
                    operation.after_state = {"error": "writeback_target_unavailable"}
                elif batch.selection and not _snapshot_matches_operation(
                    asset, snapshot, operation
                ):
                    operation.state = WritebackState.CONFLICT
                    operation.after_state = {"error": "curation_evidence_changed"}
                    event_suffix = "conflict"
                elif (
                    batch.selection
                    and operation.operation_type == "rating"
                    and current_revision is not None
                    and current_revision.rating not in {None, 0}
                ):
                    operation.state = WritebackState.CONFLICT
                    operation.after_state = {
                        "rating": current_revision.rating,
                        "error": "local_nonzero_rating_protected",
                    }
                    event_suffix = "conflict"
                elif batch.selection and (
                    not connection.write_policy.get("enabled")
                    or operation.operation_type
                    not in connection.write_policy.get("allowed_operations", [])
                ):
                    operation.state = WritebackState.FAILED
                    operation.after_state = {"error": "connection_write_policy_denied"}
                else:
                    credential = await get_active_credential(
                        session,
                        connection_id=operation.connection_id,
                        kind=CredentialKind.READ,
                    )
                    if credential is None:
                        operation.state = WritebackState.FAILED
                        operation.after_state = {"error": "write_credential_unavailable"}
                    else:
                        # Persist the immutable local precondition and renew the
                        # batch lease before any Immich request. If the process
                        # dies after a remote write but before the local result
                        # commit, compare-before-write makes the retry observe
                        # the already-current value without duplicating it.
                        # No PostgreSQL transaction is held during network I/O.
                        batch.execution_lease_expires_at = utc_now() + timedelta(minutes=5)
                        await session.commit()
                        await asyncio.sleep(batch.selection.get("interval_ms", 1000) / 1000)
                        try:
                            client_key = (connection.id, credential.kind)
                            client = clients.get(client_key)
                            if client is None:
                                client = await stack.enter_async_context(
                                    ImmichClient(
                                        instance.base_url,
                                        cipher.decrypt(credential.encrypted_secret),
                                    )
                                )
                                clients[client_key] = client
                            remote = await client.get_asset(asset.immich_asset_id)
                            if operation.operation_type == "rating":
                                requested_rating = operation.requested_state.get("rating")
                                if (
                                    isinstance(requested_rating, bool)
                                    or not isinstance(requested_rating, int)
                                    or not 1 <= requested_rating <= 5
                                ):
                                    operation.state = WritebackState.FAILED
                                    operation.after_state = {"error": "invalid_requested_rating"}
                                else:
                                    raw_remote_rating = _remote_rating_value(remote)
                                    remote_rating = _remote_rating(remote)
                                    if remote_rating == requested_rating:
                                        operation.state = WritebackState.SUCCEEDED
                                        operation.after_state = {
                                            "rating": remote_rating,
                                            "already_current": True,
                                            "write_performed": False,
                                        }
                                        event_suffix = "succeeded"
                                    elif raw_remote_rating is not None and raw_remote_rating != 0:
                                        operation.state = WritebackState.CONFLICT
                                        operation.after_state = {
                                            "rating": (remote_rating or "nonzero_unrecognized"),
                                            "error": "remote_nonzero_rating_protected",
                                            "write_performed": False,
                                        }
                                        event_suffix = "conflict"
                                    else:
                                        await client.update_rating(
                                            asset.immich_asset_id,
                                            requested_rating,
                                        )
                                        operation.state = WritebackState.SUCCEEDED
                                        operation.after_state = {
                                            "rating": requested_rating,
                                            "write_performed": True,
                                        }
                                        event_suffix = "succeeded"
                            elif operation.operation_type == "description":
                                requested_description = operation.requested_state.get("description")
                                if (
                                    not isinstance(requested_description, str)
                                    or not requested_description.strip()
                                ):
                                    operation.state = WritebackState.FAILED
                                    operation.after_state = {
                                        "error": "invalid_requested_description"
                                    }
                                else:
                                    remote_description = _remote_description(remote)
                                    if remote_description == requested_description.strip():
                                        operation.state = WritebackState.SUCCEEDED
                                        operation.after_state = {
                                            "description": remote_description,
                                            "already_current": True,
                                            "write_performed": False,
                                        }
                                        event_suffix = "succeeded"
                                    elif remote_description is not None:
                                        operation.state = WritebackState.CONFLICT
                                        operation.after_state = {
                                            "description": remote_description,
                                            "error": "remote_description_protected",
                                            "write_performed": False,
                                        }
                                        event_suffix = "conflict"
                                    else:
                                        await client.update_description(
                                            asset.immich_asset_id,
                                            requested_description,
                                        )
                                        operation.state = WritebackState.SUCCEEDED
                                        operation.after_state = {
                                            "description": requested_description,
                                            "write_performed": True,
                                        }
                                        event_suffix = "succeeded"
                            elif operation.operation_type == "tags":
                                requested_tags = operation.requested_state.get("tags")
                                if (
                                    not isinstance(requested_tags, list)
                                    or not requested_tags
                                    or any(
                                        not isinstance(tag, str) or not tag.startswith("star/")
                                        for tag in requested_tags
                                    )
                                ):
                                    operation.state = WritebackState.FAILED
                                    operation.after_state = {"error": "invalid_requested_tags"}
                                else:
                                    existing_names = _remote_tag_names(remote)
                                    missing_names = [
                                        tag for tag in requested_tags if tag not in existing_names
                                    ]
                                    if missing_names:
                                        all_tags = await client.get_tags()
                                        tags_by_name = {
                                            str(tag.get("value") or tag.get("name")): str(
                                                tag.get("id")
                                            )
                                            for tag in all_tags
                                            if isinstance(tag.get("name"), str)
                                            and isinstance(tag.get("id"), str)
                                        }
                                        tag_ids: list[str] = []
                                        for name in missing_names:
                                            tag_id = tags_by_name.get(name)
                                            if tag_id is None:
                                                created_tag = await client.create_tag(name)
                                                tag_id = str(created_tag["id"])
                                            tag_ids.append(tag_id)
                                        await client.add_tags_to_asset(
                                            asset.immich_asset_id,
                                            tag_ids,
                                        )
                                    operation.state = WritebackState.SUCCEEDED
                                    operation.after_state = {
                                        "tags": sorted(existing_names | set(requested_tags)),
                                        "added": missing_names,
                                        "write_performed": bool(missing_names),
                                    }
                                    event_suffix = "succeeded"
                        except (ImmichApiError, httpx.HTTPError, ValueError) as exc:
                            code = getattr(exc, "status_code", None)
                            if isinstance(exc, httpx.HTTPStatusError):
                                code = exc.response.status_code
                            retryable = (
                                not isinstance(exc, ValueError)
                                and (code is None or code == 429 or code >= 500)
                                and attempt_number < 3
                            )
                            if retryable:
                                operation.state = WritebackState.PREVIEWED
                                event_suffix = "retrying"
                                retry_not_before = utc_now() + timedelta(
                                    seconds=max(
                                        15 * 2 ** (attempt_number - 1),
                                        getattr(exc, "retry_after_seconds", 0),
                                    )
                                )
                            else:
                                operation.state = WritebackState.FAILED
                            operation.after_state = {
                                "error": type(exc).__name__,
                                "retryable": retryable,
                                "retry_at": (
                                    retry_not_before.isoformat() if retry_not_before else None
                                ),
                            }
                operation.attempt_count = attempt_number
                await append_audit_event(
                    session,
                    AuditRecord(
                        event_type=f"immich.writeback.{event_suffix}",
                        actor_type="service",
                        actor_id="writeback-worker",
                        target_ref={
                            "batch_id": str(batch.id),
                            "operation_id": str(operation.id),
                            "asset_id": str(operation.asset_id),
                            "connection_id": str(operation.connection_id),
                        },
                        correlation_id=str(batch.id),
                        before=operation.before_state,
                        after=operation.after_state,
                        details={
                            "operation_type": operation.operation_type,
                            "attempt": attempt_number,
                        },
                    ),
                )

                if retry_not_before is not None:
                    break

    counts = await refresh_batch_operation_counts(session, batch)
    pending_count = int(counts.get(WritebackState.PREVIEWED.value, 0)) + int(
        counts.get(WritebackState.APPROVED.value, 0)
    )
    if pending_count == 0:
        if counts.get(WritebackState.FAILED.value, 0):
            batch.state = WritebackState.FAILED
        elif counts.get(WritebackState.CONFLICT.value, 0):
            batch.state = WritebackState.CONFLICT
        else:
            batch.state = WritebackState.SUCCEEDED
        await append_operation_event(
            session,
            OperationRecord(
                source="writeback",
                event_type="writeback.batch.completed",
                message="Rating write-back completed",
                severity=(
                    "warning"
                    if batch.state in {WritebackState.CONFLICT, WritebackState.FAILED}
                    else "info"
                ),
                correlation_id=str(batch.id),
                details={"counts": counts, "state": batch.state.value},
            ),
        )
    batch.execution_lease_id = None
    batch.execution_lease_expires_at = retry_not_before
    await session.commit()
    return True
