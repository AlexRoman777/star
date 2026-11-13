"""Classify failed work so the owner sees what matters and what to do.

Every terminal failure and every retry-waiting task in the window is grouped
by its error class. Each class carries a kind (source, runtime, model, code,
benign), a severity, the retry rule in force, and the action to take, so the
Failures page never leaves the owner guessing whether something needs a code
fix. Photos that fail repeatedly across tasks are listed separately: a
photograph the pipeline keeps returning to is the edge case that wastes
compute, and it deserves a decision rather than another attempt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import Asset, AssetRevision, Task
from star.domain.enums import TaskState
from star.domain.schemas.failures import (
    FailedPhotoResponse,
    FailureClassResponse,
    FailureKind,
    FailureOverviewResponse,
    FailureSeverity,
)

DEFAULT_WINDOW = timedelta(hours=72)
SAMPLE_PHOTOS = 5
REPEAT_THRESHOLD = 3


@dataclass(frozen=True)
class FailureRule:
    kind: FailureKind
    severity: FailureSeverity
    title: str
    meaning: str
    retry_policy: str
    action: str
    code_fix_likely: bool = False


RULES: dict[str, FailureRule] = {
    "SourceMediaUnavailableError": FailureRule(
        "source",
        "info",
        "Immich could not serve the photo",
        "Immich answered 404 or an empty preview for this asset: it was deleted, trashed, "
        "moved between libraries, or its preview has not been generated yet.",
        "Retried on the next pass only after the asset is seen again in a sync; not retried "
        "in place. League entries for it are marked unavailable and recover after six hours.",
        "Nothing to fix in Star. Check the photo in Immich if it should exist.",
    ),
    "ControlPlanePayloadRejected": FailureRule(
        "code",
        "attention",
        "The coordinator rejected a worker result",
        "A worker submitted a result the NAS refused (contract, model registration or "
        "media provenance mismatch). Since v256 a missing preview is reported as a source "
        "failure instead, so this class is a code or deployment problem.",
        "Up to the task's attempt limit; a rejected senior registration is repaired by the "
        "scheduler and re-queued once.",
        "Read the reason on the task. A registration or contract reason needs a code or "
        "deployment fix; a preview reason is a source problem.",
        code_fix_likely=True,
    ),
    "PhotoComputeBudgetExceeded": FailureRule(
        "model",
        "attention",
        "The photo used up its compute budget",
        "Successive slow attempts on this photograph spent the 1,200 s safety envelope; "
        "the model is stuck or the image is pathological for it.",
        "Not retried automatically: the budget is per photo. A re-review resets it.",
        "If many photos hit this, the model or its settings are the problem; one photo is "
        "an outlier to re-review manually.",
    ),
    "StructuredOutputRejected": FailureRule(
        "model",
        "attention",
        "The model could not produce valid JSON",
        "After in-request correction the answer still failed the schema.",
        "One fresh attempt cycle after the first terminal failure, then terminal.",
        "A cluster means the prompt or schema regressed; a single photo is a model quirk.",
    ),
    "LocalInferenceTransportError": FailureRule(
        "runtime",
        "attention",
        "The local model runtime did not answer",
        "Ollama, MLX or the Bonsai runtime disconnected, timed out or returned an "
        "invalid transport response.",
        "Retried up to the attempt limit with backoff; the worker marks the runtime unhealthy "
        "and stops leasing until it answers again.",
        "Check the runtime container or service on that machine.",
    ),
    "AnalysisGenerationSuperseded": FailureRule(
        "benign",
        "info",
        "Superseded before it ran",
        "A newer review generation replaced this one, so the stale task was cancelled.",
        "Never retried; the newer generation carries the work.",
        "Nothing to do.",
    ),
    "TaskLeaseExpired": FailureRule(
        "runtime",
        "info",
        "A lease ran out",
        "The worker holding this task stopped heartbeating or the task overran its wall clock.",
        "Re-queued while attempts remain; the attempt is not counted when the worker restarted.",
        "Nothing to do unless it repeats on one machine.",
    ),
}

RULES.update(
    {
        "SourceMediaQuarantined": FailureRule(
            "source",
            "info",
            "Photo quarantined after repeated source failures",
            "Immich served an unusable or missing derivative twice; Star stopped scheduling "
            "work for the photo.",
            "Never retried automatically; Re-review offers a quarantine restore.",
            "Check the photo in Immich; restore it from the inspector if it is fine.",
        ),
        "WorkerTaskContractError": FailureRule(
            "code",
            "attention",
            "Worker and NAS disagreed on a task contract",
            "A worker ran a task whose contract revision the NAS no longer accepts, usually "
            "across a release.",
            "Not retried until the worker runs the release the NAS expects.",
            "Deploy the workers for the current release; if it repeats on one release, the "
            "contract hash is wrong in code.",
            code_fix_likely=True,
        ),
        "ControlPlaneHttpError": FailureRule(
            "runtime",
            "attention",
            "The NAS did not answer a worker",
            "A 5xx or network error between a worker and the NAS while a task ran.",
            "Retried up to the attempt limit with backoff.",
            "Look at the NAS API health and the worker's network; a burst means the NAS was "
            "restarting or overloaded.",
        ),
        "LeaseExpired": FailureRule(
            "runtime",
            "info",
            "A lease ran out",
            "The worker holding this task stopped heartbeating or the task overran its wall "
            "clock, often during a deploy restart.",
            "Re-queued while attempts remain; a restart's lost leases are not counted.",
            "Nothing to do unless it repeats on one machine outside deploys.",
        ),
        "LocalInferenceJsonError": FailureRule(
            "model",
            "attention",
            "The model answered with invalid JSON",
            "The runtime returned text the worker could not parse as the contract's JSON.",
            "Retried up to the attempt limit; a photo that fails twice this way is "
            "quarantined for analysis.",
            "A cluster means the prompt, schema or runtime regressed; a single case is a "
            "model quirk.",
        ),
        "LocalInferenceSchemaError": FailureRule(
            "model",
            "attention",
            "The model's JSON missed the schema",
            "Valid JSON that did not satisfy the contract schema after correction.",
            "Retried up to the attempt limit; twice on one photo quarantines it for analysis.",
            "A cluster means the prompt or schema regressed.",
        ),
        "LocalInferenceContextWindowError": FailureRule(
            "model",
            "attention",
            "The request did not fit the model's context",
            "A sheet or prompt exceeded the runtime's context window.",
            "Retried once on the contract's fallback envelope, then terminal.",
            "Lower the sheet size or image edge for that runtime in the task contract.",
            code_fix_likely=True,
        ),
        "LocalInferenceRequestError": FailureRule(
            "runtime",
            "attention",
            "The model runtime refused the request",
            "The local runtime answered with an error for the request itself.",
            "Retried up to the attempt limit.",
            "Read the runtime's log on that machine.",
        ),
        "TaskCancelled": FailureRule(
            "benign",
            "info",
            "Cancelled by the owner or a newer plan",
            "The task was cancelled before or while it ran.",
            "Never retried.",
            "Nothing to do.",
        ),
        "StageNotApplicable": FailureRule(
            "benign",
            "info",
            "Stage no longer applies",
            "The photo's routing changed before this stage ran.",
            "Never retried.",
            "Nothing to do.",
        ),
        "ObsoleteLibraryGeneration": FailureRule(
            "benign",
            "info",
            "Belongs to an obsolete generation",
            "A newer library generation replaced the one this task served.",
            "Never retried.",
            "Nothing to do.",
        ),
        "QwenNoLongerRequiredByPolicy": FailureRule(
            "benign",
            "info",
            "Advanced review no longer required",
            "The routing policy changed after the task was queued.",
            "Never retried.",
            "Nothing to do.",
        ),
        "ValueError": FailureRule(
            "code",
            "attention",
            "A worker hit an unexpected value",
            "An unhandled ValueError inside a worker executor.",
            "Retried up to the attempt limit.",
            "Read the worker log; this is a bug to fix in code.",
            code_fix_likely=True,
        ),
    }
)

# Every class Star has produced in the last thirty days of production, so
# the contract test can prove none of them is unclassified.
KNOWN_ERROR_CLASSES = frozenset(RULES)

UNKNOWN_RULE = FailureRule(
    "unknown",
    "attention",
    "Unclassified failure",
    "Star has no rule for this error class yet.",
    "The task's own attempt limit applies.",
    "Read the error and add a rule: unclassified failures are the ones that hide problems.",
    code_fix_likely=True,
)


def rule_for(error_class: str) -> FailureRule:
    if error_class in RULES:
        return RULES[error_class]
    lowered = error_class.lower()
    if "unavailable" in lowered or "notfound" in lowered or "404" in lowered:
        return RULES["SourceMediaUnavailableError"]
    if "structured" in lowered or "schema" in lowered or "json" in lowered:
        return RULES["StructuredOutputRejected"]
    if "transport" in lowered or "timeout" in lowered or "connection" in lowered:
        return RULES["LocalInferenceTransportError"]
    if "superseded" in lowered or "cancelled" in lowered:
        return RULES["AnalysisGenerationSuperseded"]
    if "budget" in lowered:
        return RULES["PhotoComputeBudgetExceeded"]
    return UNKNOWN_RULE


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class _ClassAggregate:
    failed: int = 0
    retrying: int = 0
    types: set[str] = field(default_factory=set)
    first: datetime | None = None
    last: datetime | None = None
    assets: list[uuid.UUID] = field(default_factory=list)


@dataclass
class _PhotoAggregate:
    failed: int = 0
    classes: set[str] = field(default_factory=set)
    last: datetime | None = None


async def failure_overview(
    session: AsyncSession, *, window: timedelta = DEFAULT_WINDOW
) -> FailureOverviewResponse:
    now = utc_now()
    since = now - window
    rows = list(
        (
            await session.execute(
                select(
                    Task.last_error_class,
                    Task.state,
                    Task.task_type,
                    Task.asset_id,
                    Task.updated_at,
                )
                .where(
                    Task.state.in_((TaskState.FAILED, TaskState.RETRY_WAITING)),
                    Task.updated_at >= since,
                    Task.last_error_class.is_not(None),
                )
                .order_by(Task.updated_at.desc())
                .limit(20_000)
            )
        ).all()
    )
    by_class: dict[str, _ClassAggregate] = {}
    per_asset: dict[uuid.UUID, _PhotoAggregate] = {}
    for error_class, state, task_type, asset_id, updated_at in rows:
        key = str(error_class)
        entry = by_class.setdefault(key, _ClassAggregate())
        if state == TaskState.FAILED:
            entry.failed += 1
        else:
            entry.retrying += 1
        entry.types.add(str(task_type))
        stamped = _as_utc(updated_at)
        if stamped is not None:
            if entry.first is None or stamped < entry.first:
                entry.first = stamped
            if entry.last is None or stamped > entry.last:
                entry.last = stamped
        if asset_id is not None:
            if asset_id not in entry.assets and len(entry.assets) < SAMPLE_PHOTOS:
                entry.assets.append(asset_id)
            if state == TaskState.FAILED:
                photo_entry = per_asset.setdefault(asset_id, _PhotoAggregate())
                photo_entry.failed += 1
                photo_entry.classes.add(key)
                if stamped is not None and (photo_entry.last is None or stamped > photo_entry.last):
                    photo_entry.last = stamped
    sample_ids = {asset_id for entry in by_class.values() for asset_id in entry.assets}
    repeat_ids = {
        asset_id for asset_id, item in per_asset.items() if item.failed >= REPEAT_THRESHOLD
    }
    wanted = list(sample_ids | repeat_ids)
    names: dict[uuid.UUID, tuple[str, str | None]] = {}
    if wanted:
        asset_rows = await session.execute(
            select(Asset.id, Asset.immich_asset_id, AssetRevision.normalized_metadata)
            .outerjoin(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(Asset.id.in_(wanted))
        )
        for asset_id, immich_id, metadata in asset_rows.all():
            filename = None
            if isinstance(metadata, dict):
                raw = metadata.get("originalFileName") or metadata.get("filename")
                filename = str(raw) if raw else None
            names[asset_id] = (str(immich_id), filename)

    def photo(asset_id: uuid.UUID) -> FailedPhotoResponse:
        immich_id, filename = names.get(asset_id, (None, None))
        info = per_asset.get(asset_id, _PhotoAggregate())
        return FailedPhotoResponse(
            asset_id=str(asset_id),
            immich_asset_id=immich_id,
            filename=filename,
            failed_tasks=info.failed,
            classes=sorted(info.classes),
            last_failed_at=info.last,
        )

    classes: list[FailureClassResponse] = []
    for key, entry in by_class.items():
        rule = rule_for(key)
        classes.append(
            FailureClassResponse(
                error_class=key,
                kind=rule.kind,
                severity=rule.severity,
                title=rule.title,
                meaning=rule.meaning,
                retry_policy=rule.retry_policy,
                action=rule.action,
                code_fix_likely=rule.code_fix_likely,
                count=entry.failed,
                retrying=entry.retrying,
                task_types=sorted(entry.types),
                first_seen_at=entry.first,
                last_seen_at=entry.last,
                sample_photos=[photo(asset_id) for asset_id in entry.assets],
            )
        )
    order = {"urgent": 0, "attention": 1, "info": 2}
    # A class becomes urgent when it dominates: a code or runtime problem that
    # keeps producing failures is not "attention", it is blocking the batch.
    total_failed = sum(item.count for item in classes)
    for item in classes:
        if (
            item.kind in {"code", "runtime"}
            and item.count >= 25
            and item.count * 4 >= max(total_failed, 1)
        ):
            item.severity = "urgent"
    classes.sort(key=lambda item: (order[item.severity], -item.count))
    repeat = sorted(
        (photo(asset_id) for asset_id in repeat_ids),
        key=lambda item: -item.failed_tasks,
    )[:50]
    urgent = sum(1 for item in classes if item.severity == "urgent")
    attention = sum(1 for item in classes if item.severity == "attention")
    if not classes:
        summary = "No failed or retrying work in the window."
    elif urgent:
        plural = "es" if urgent != 1 else ""
        summary = f"{urgent} failure class{plural} need{'s' if urgent == 1 else ''} a fix now."
    elif attention:
        plural = "es" if attention != 1 else ""
        summary = f"{attention} class{plural} worth a look; nothing is blocking the batch."
    else:
        summary = "Only source and benign failures; nothing to fix in Star."
    return FailureOverviewResponse(
        observed_at=now,
        window_hours=int(window.total_seconds() // 3600),
        total_failed=total_failed,
        total_retrying=sum(item.retrying for item in classes),
        urgent=urgent,
        attention=attention,
        summary=summary,
        classes=classes,
        repeat_offenders=repeat,
    )
