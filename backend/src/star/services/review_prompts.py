"""Owner-editable review prompts and full-library re-analysis runs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Uuid, and_, exists, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.asset_vlm_submission import (
    SENIOR_REVIEW_RUNTIMES,
    eligible_asset_vlm_models,
    enqueue_primary_review,
)
from star.core.observability import record_gate
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    CollectionGroupMember,
    LibraryProgram,
    ReviewPromptOverride,
    ReviewPromptRevision,
    ReviewReanalysisRun,
    Task,
    User,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import LibraryProgramState, MediaType, Role, TaskState
from star.inference.policy import (
    ALLOWED_TASKS,
    APPLE_SYSTEM_PROMPT_KEY,
    CANONICAL_OWNER_TASTE,
    CANONICAL_RATING_SCALE,
    DEEP_REVIEW_PROMPTS,
    MAXIMUM_OWNER_TASTE_EDITOR_CHARACTERS,
    MAXIMUM_RATING_SCALE_CHARACTERS,
    MINIMUM_OWNER_TASTE_CHARACTERS,
    MINIMUM_RATING_SCALE_CHARACTERS,
    MODEL_SYSTEM_PROMPTS,
    NVIDIA_SYSTEM_PROMPT_KEY,
    OWNER_TASTE_PROMPT_KEY,
    RATING_SCALE_PROMPT_KEY,
    validated_owner_taste,
    validated_rating_scale,
)
from star.services.analysis_generations import (
    bind_generation_task,
    create_analysis_generation,
    current_analysis_signature,
    expanded_stages,
    prompt_version_signature,
)
from star.services.campaigns import canonical_digest
from star.services.reanalysis_lineage import (
    reanalysis_generation_ids,
    reanalysis_task_filter,
)

MINIMUM_PROMPT_CHARACTERS = 200
MAXIMUM_PROMPT_CHARACTERS = 24_000
MINIMUM_SYSTEM_PROMPT_CHARACTERS = 40
MAXIMUM_SYSTEM_PROMPT_CHARACTERS = 4_000
FORBIDDEN_PROMPT_MARKERS = ("<untrusted-context>", "</untrusted-context>")
# The work a re-review wave must not compete with: the reviews themselves.
# Media staging and embeddings are support runways that never empty.
REVIEW_TASK_TYPES = frozenset({"asset.primary_review", "asset.deep_review"})
REANALYSIS_CHUNK_SIZE = 250
REANALYSIS_HOT_LIMIT = 400
REANALYSIS_PRIORITY = 62


@dataclass(frozen=True)
class PromptDefinition:
    key: str
    label: str
    machine: Literal["nvidia", "apple", "shared"]
    stage: Literal["system_role", "primary_review", "final_review", "rating_scale", "owner_taste"]
    description: str


def prompt_definitions() -> tuple[PromptDefinition, ...]:
    """Return every prompt the owner may edit, newest review contract first."""

    definitions = [
        PromptDefinition(
            key=OWNER_TASTE_PROMPT_KEY,
            label="My taste (shared by every rating model)",
            machine="shared",
            stage="owner_taste",
            description=(
                "Your own preferences in plain words: what you show, print, keep, or cull. It is "
                "sent with the shared scale to the individual review, the quick four-photo group, "
                "and the Highlight comparison so every model rates the way you would."
            ),
        ),
        PromptDefinition(
            key=RATING_SCALE_PROMPT_KEY,
            label="Star rating scale (shared by every model)",
            machine="shared",
            stage="rating_scale",
            description=(
                "One definition of 1 to 5 stars, injected into the NVIDIA and Apple prompts and "
                "into the worker context. Editing it here keeps every model on the same scale."
            ),
        ),
        PromptDefinition(
            key=NVIDIA_SYSTEM_PROMPT_KEY,
            label="NVIDIA model role",
            machine="nvidia",
            stage="system_role",
            description=("The exact system message sent before every NVIDIA review instruction."),
        ),
        PromptDefinition(
            key="primary_review",
            label="Primary photo review (nvidia)",
            machine="nvidia",
            stage="primary_review",
            description=(
                "The independent NVIDIA pass that produces the first complete review and star "
                "proposal for every photo."
            ),
        ),
        PromptDefinition(
            key=APPLE_SYSTEM_PROMPT_KEY,
            label="Advanced review model role",
            machine="apple",
            stage="system_role",
            description=(
                "The exact system message sent before every Advanced (senior) review instruction."
            ),
        ),
    ]
    for version, metadata in DEEP_REVIEW_PROMPTS.items():
        if not bool(metadata.get("production_eligible")):
            continue
        definitions.append(
            PromptDefinition(
                key=f"photo_deep_review:{version}",
                label="Final photo review (Advanced Qwen3.8 27B)",
                machine="apple",
                stage="final_review",
                description=str(metadata.get("purpose", "")),
            )
        )
    return tuple(definitions)


def prompt_definition(prompt_key: str) -> PromptDefinition | None:
    return next((item for item in prompt_definitions() if item.key == prompt_key), None)


def default_prompt_text(prompt_key: str) -> str:
    if prompt_key == OWNER_TASTE_PROMPT_KEY:
        return CANONICAL_OWNER_TASTE
    if prompt_key == RATING_SCALE_PROMPT_KEY:
        return CANONICAL_RATING_SCALE
    if prompt_key in MODEL_SYSTEM_PROMPTS:
        return MODEL_SYSTEM_PROMPTS[prompt_key]
    return str(ALLOWED_TASKS[prompt_key])


def validate_prompt_text(instruction: str) -> str:
    text = instruction.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < MINIMUM_PROMPT_CHARACTERS:
        raise ValueError(f"a review prompt needs at least {MINIMUM_PROMPT_CHARACTERS} characters")
    if len(text) > MAXIMUM_PROMPT_CHARACTERS:
        raise ValueError(f"a review prompt cannot exceed {MAXIMUM_PROMPT_CHARACTERS} characters")
    lowered = text.lower()
    if any(marker in lowered for marker in FORBIDDEN_PROMPT_MARKERS):
        raise ValueError("a review prompt cannot contain the untrusted-context markers")
    if any(character in text for character in ("\x00", "\x1b")):
        raise ValueError("a review prompt cannot contain control characters")
    return text


def prompt_bounds(prompt_key: str) -> tuple[int, int]:
    """Return the inclusive character bounds the interface must enforce."""

    if prompt_key == OWNER_TASTE_PROMPT_KEY:
        return MINIMUM_OWNER_TASTE_CHARACTERS, MAXIMUM_OWNER_TASTE_EDITOR_CHARACTERS
    if prompt_key == RATING_SCALE_PROMPT_KEY:
        return MINIMUM_RATING_SCALE_CHARACTERS, MAXIMUM_RATING_SCALE_CHARACTERS
    if prompt_key in MODEL_SYSTEM_PROMPTS:
        return MINIMUM_SYSTEM_PROMPT_CHARACTERS, MAXIMUM_SYSTEM_PROMPT_CHARACTERS
    return MINIMUM_PROMPT_CHARACTERS, MAXIMUM_PROMPT_CHARACTERS


async def override_map(session: AsyncSession) -> dict[str, ReviewPromptOverride]:
    rows = (await session.scalars(select(ReviewPromptOverride))).all()
    return {row.prompt_key: row for row in rows}


async def lock_review_prompt_catalog(session: AsyncSession) -> None:
    """Serialize prompt baseline, suggestion, and activation changes on PostgreSQL."""

    if session.get_bind().dialect.name == "postgresql":
        await session.execute(select(func.pg_advisory_xact_lock(1_947_726_673)))


async def prompt_revision(session: AsyncSession, prompt_key: str | None = None) -> int:
    override_query = select(func.max(ReviewPromptOverride.revision))
    history_query = select(func.max(ReviewPromptRevision.version))
    if prompt_key is not None:
        override_query = override_query.where(ReviewPromptOverride.prompt_key == prompt_key)
        history_query = history_query.where(ReviewPromptRevision.prompt_key == prompt_key)
    override_value = int(await session.scalar(override_query) or 0)
    history_value = int(await session.scalar(history_query) or 0)
    return max(override_value, history_value)


async def effective_prompt_text(session: AsyncSession, prompt_key: str) -> str | None:
    """Return the owner text for a prompt, or the built-in default."""

    if (
        prompt_key not in {RATING_SCALE_PROMPT_KEY, OWNER_TASTE_PROMPT_KEY}
        and prompt_key not in ALLOWED_TASKS
        and prompt_key not in MODEL_SYSTEM_PROMPTS
    ):
        return None
    override = await session.scalar(
        select(ReviewPromptOverride).where(ReviewPromptOverride.prompt_key == prompt_key)
    )
    text = override.instruction if override is not None else default_prompt_text(prompt_key)
    if prompt_key == OWNER_TASTE_PROMPT_KEY:
        return with_taste_calibration(text, await session.scalar(select(LibraryProgram).limit(1)))
    return text


def with_taste_calibration(owner_text: str, program: LibraryProgram | None) -> str:
    """Append the derived correction block so the models learn from owner overrides."""

    from star.services.taste_calibration import (
        CALIBRATION_HEADING,
        taste_calibration_text,
    )

    calibration = taste_calibration_text(program)
    if not calibration:
        return owner_text
    return f"{owner_text.rstrip()}\n\n{CALIBRATION_HEADING}\n{calibration}"


async def prompt_text_at_revision(
    session: AsyncSession,
    *,
    prompt_key: str,
    version: int,
) -> str | None:
    """Return one immutable saved prompt revision for a worker replay."""

    revision = await session.scalar(
        select(ReviewPromptRevision).where(
            ReviewPromptRevision.prompt_key == prompt_key,
            ReviewPromptRevision.version == version,
        )
    )
    return revision.instruction if revision is not None else None


async def resolve_reanalysis_prompt_snapshot(
    session: AsyncSession,
    requested_version: str,
) -> tuple[str, dict[str, int]]:
    """Resolve current or historical prompt provenance before a run starts."""

    if requested_version == "current":
        rows = list(
            (
                await session.execute(
                    select(ReviewPromptRevision.prompt_key, ReviewPromptRevision.version).where(
                        ReviewPromptRevision.status == "active"
                    )
                )
            ).all()
        )
        revisions = {str(key): int(version) for key, version in rows}
    else:
        stored = await session.scalar(
            select(AnalysisGeneration.prompt_revisions)
            .where(AnalysisGeneration.prompt_version == requested_version)
            .order_by(AnalysisGeneration.created_at.desc(), AnalysisGeneration.id.desc())
            .limit(1)
        )
        if not isinstance(stored, dict) or not stored:
            raise ValueError("the selected prompt version has no retained revision snapshot")
        revisions = {
            str(key): int(version)
            for key, version in stored.items()
            if isinstance(key, str)
            and isinstance(version, int)
            and not isinstance(version, bool)
            and version > 0
        }
    review_revision = revisions.get("primary_review")
    rating_revision = revisions.get(RATING_SCALE_PROMPT_KEY)
    if review_revision is None or rating_revision is None:
        raise ValueError("the selected prompt version is missing its review or rating revision")
    available = int(
        await session.scalar(
            select(func.count(ReviewPromptRevision.id)).where(
                or_(
                    *[
                        and_(
                            ReviewPromptRevision.prompt_key == key,
                            ReviewPromptRevision.version == version,
                        )
                        for key, version in revisions.items()
                    ]
                )
            )
        )
        or 0
    )
    if available != len(revisions):
        raise ValueError("the selected prompt version is incomplete and cannot be replayed")
    signature = prompt_version_signature(revisions)
    if requested_version != "current" and requested_version != signature:
        raise ValueError("the selected prompt signature does not match its retained revisions")
    return signature, revisions


def _validated_prompt_for_key(prompt_key: str, instruction: str) -> str:
    if prompt_key == OWNER_TASTE_PROMPT_KEY:
        return validated_owner_taste(instruction)
    if prompt_key == RATING_SCALE_PROMPT_KEY:
        return validated_rating_scale(instruction)
    if prompt_key in MODEL_SYSTEM_PROMPTS:
        text = instruction.replace("\r\n", "\n").replace("\r", "\n").strip()
        minimum, maximum = prompt_bounds(prompt_key)
        if not minimum <= len(text) <= maximum:
            raise ValueError(f"a system prompt must be between {minimum} and {maximum} characters")
        if any(marker in text.lower() for marker in FORBIDDEN_PROMPT_MARKERS):
            raise ValueError("a system prompt cannot contain the untrusted-context markers")
        if any(character in text for character in ("\x00", "\x1b")):
            raise ValueError("a system prompt cannot contain control characters")
        return text
    return validate_prompt_text(instruction)


async def _next_prompt_version(session: AsyncSession, prompt_key: str) -> int:
    return (
        int(
            await session.scalar(
                select(func.max(ReviewPromptRevision.version)).where(
                    ReviewPromptRevision.prompt_key == prompt_key
                )
            )
            or 0
        )
        + 1
    )


async def _retire_active_prompt_revision(session: AsyncSession, prompt_key: str) -> None:
    active = await session.scalar(
        select(ReviewPromptRevision).where(
            ReviewPromptRevision.prompt_key == prompt_key,
            ReviewPromptRevision.status == "active",
        )
    )
    if active is not None:
        active.status = "retired"


async def record_active_prompt_revision(
    session: AsyncSession,
    *,
    prompt_key: str,
    instruction: str,
    source: Literal["built_in", "owner"],
    actor_id: uuid.UUID | None,
    change_summary: str,
) -> ReviewPromptRevision:
    text = _validated_prompt_for_key(prompt_key, instruction)
    await _retire_active_prompt_revision(session, prompt_key)
    revision = ReviewPromptRevision(
        prompt_key=prompt_key,
        version=await _next_prompt_version(session, prompt_key),
        instruction=text,
        instruction_digest=canonical_digest(text),
        source=source,
        status="active",
        change_summary=change_summary,
        evidence={},
        created_by=actor_id,
        activated_by=actor_id,
        activated_at=utc_now(),
    )
    session.add(revision)
    await session.flush()
    return revision


def _rebase_generated_rating_prompt(
    instruction: str,
    *,
    built_in: str,
) -> str | None:
    """Move an approved generated appendix onto the released rating-scale baseline."""

    for heading in ("Private preference guidance", "Private preference suggestion"):
        marker = f"\n\n{heading}\n"
        if marker not in instruction:
            continue
        appendix = instruction.split(marker, 1)[1].strip()
        if not appendix:
            return None
        return _validated_prompt_for_key(
            RATING_SCALE_PROMPT_KEY,
            built_in.rstrip() + marker + appendix,
        )
    return None


async def ensure_prompt_revision_history(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
) -> None:
    await lock_review_prompt_catalog(session)
    overrides = await override_map(session)
    for definition in prompt_definitions():
        active = await session.scalar(
            select(ReviewPromptRevision).where(
                ReviewPromptRevision.prompt_key == definition.key,
                ReviewPromptRevision.status == "active",
            )
        )
        override = overrides.get(definition.key)
        default = default_prompt_text(definition.key)
        if active is not None:
            if (
                definition.key == RATING_SCALE_PROMPT_KEY
                and override is not None
                and active.source in {"personal_llm", "personal_ranker"}
            ):
                rebased = _rebase_generated_rating_prompt(
                    override.instruction,
                    built_in=default,
                )
                if rebased is not None and canonical_digest(rebased) != active.instruction_digest:
                    active.status = "retired"
                    override.instruction = rebased
                    override.revision = await prompt_revision(session, definition.key) + 1
                    override.updated_by = actor_id
                    revision = ReviewPromptRevision(
                        prompt_key=definition.key,
                        version=await _next_prompt_version(session, definition.key),
                        instruction=rebased,
                        instruction_digest=canonical_digest(rebased),
                        source=active.source,
                        status="active",
                        change_summary=(
                            "Rebased the approved private guidance onto the released "
                            "family-memory rating scale."
                        ),
                        evidence={
                            **dict(active.evidence),
                            "rebased_from_revision_id": str(active.id),
                            "built_in_digest": canonical_digest(default),
                        },
                        personal_ranker_version_id=active.personal_ranker_version_id,
                        created_by=actor_id,
                        activated_by=actor_id,
                        activated_at=utc_now(),
                    )
                    session.add(revision)
                    await session.flush()
                continue
            if (
                override is None
                and active.source == "built_in"
                and active.instruction_digest != canonical_digest(default)
            ):
                await record_active_prompt_revision(
                    session,
                    prompt_key=definition.key,
                    instruction=default,
                    source="built_in",
                    actor_id=actor_id,
                    change_summary="Activated the released family-memory prompt baseline.",
                )
            continue
        await record_active_prompt_revision(
            session,
            prompt_key=definition.key,
            instruction=(override.instruction if override else default),
            source="owner" if override else "built_in",
            actor_id=actor_id,
            change_summary="Imported current prompt as the restorable baseline.",
        )


async def prompt_revision_history(session: AsyncSession) -> list[ReviewPromptRevision]:
    return list(
        await session.scalars(
            select(ReviewPromptRevision).order_by(
                ReviewPromptRevision.prompt_key,
                ReviewPromptRevision.version.desc(),
            )
        )
    )


async def record_personal_llm_prompt_revision(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    appendix: str,
    change_summary: str,
    evidence: dict[str, Any],
    personal_ranker_version_id: uuid.UUID | None,
) -> ReviewPromptRevision:
    """Record a local-LLM suggestion without granting it activation authority."""

    await lock_review_prompt_catalog(session)
    base = await effective_prompt_text(session, RATING_SCALE_PROMPT_KEY)
    if base is None:
        raise ValueError("the shared rating prompt is unavailable")
    for heading in ("\n\nPrivate preference suggestion\n", "\n\nPrivate preference guidance\n"):
        base = base.split(heading, 1)[0].rstrip()
    normalized_appendix = "\n".join(
        line.rstrip() for line in appendix.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ).strip()
    if not 200 <= len(normalized_appendix) <= 2400:
        raise ValueError("the local preference appendix must contain 200 to 2400 characters")
    if any(marker in normalized_appendix.lower() for marker in FORBIDDEN_PROMPT_MARKERS):
        raise ValueError("the local preference appendix contains a forbidden marker")
    instruction = _validated_prompt_for_key(
        RATING_SCALE_PROMPT_KEY,
        base + "\n\nPrivate preference guidance\n" + normalized_appendix,
    )
    revision = ReviewPromptRevision(
        prompt_key=RATING_SCALE_PROMPT_KEY,
        version=await _next_prompt_version(session, RATING_SCALE_PROMPT_KEY),
        instruction=instruction,
        instruction_digest=canonical_digest(instruction),
        source="personal_llm",
        status="pending",
        change_summary=change_summary[:512],
        evidence=evidence,
        personal_ranker_version_id=personal_ranker_version_id,
        created_by=actor_id,
    )
    session.add(revision)
    await session.flush()
    return revision


async def activate_prompt_revision(
    session: AsyncSession,
    *,
    revision: ReviewPromptRevision,
    actor_id: uuid.UUID,
) -> ReviewPromptRevision:
    await lock_review_prompt_catalog(session)
    if revision.status not in {"pending", "retired"}:
        raise ValueError("only a pending or retired prompt version can be activated")
    text = _validated_prompt_for_key(revision.prompt_key, revision.instruction)
    await _retire_active_prompt_revision(session, revision.prompt_key)
    override = await session.scalar(
        select(ReviewPromptOverride).where(ReviewPromptOverride.prompt_key == revision.prompt_key)
    )
    if text == _validated_prompt_for_key(
        revision.prompt_key,
        default_prompt_text(revision.prompt_key),
    ):
        if override is not None:
            await session.delete(override)
    elif override is None:
        session.add(
            ReviewPromptOverride(
                prompt_key=revision.prompt_key,
                instruction=text,
                revision=await prompt_revision(session, revision.prompt_key) + 1,
                updated_by=actor_id,
            )
        )
    else:
        override.instruction = text
        override.revision = await prompt_revision(session, revision.prompt_key) + 1
        override.updated_by = actor_id
        override.updated_at = utc_now()
    revision.status = "active"
    revision.activated_by = actor_id
    revision.activated_at = utc_now()
    await session.flush()
    return revision


async def reject_prompt_revision(session: AsyncSession, *, revision: ReviewPromptRevision) -> None:
    await lock_review_prompt_catalog(session)
    if revision.status != "pending":
        raise ValueError("only a pending prompt suggestion can be rejected")
    revision.status = "rejected"
    await session.flush()


async def save_prompt_override(
    session: AsyncSession,
    *,
    prompt_key: str,
    instruction: str,
    actor_id: uuid.UUID,
) -> ReviewPromptOverride:
    await lock_review_prompt_catalog(session)
    definition = prompt_definition(prompt_key)
    if definition is None:
        raise ValueError("this prompt cannot be edited")
    text = _validated_prompt_for_key(prompt_key, instruction)
    override = await session.scalar(
        select(ReviewPromptOverride).where(ReviewPromptOverride.prompt_key == prompt_key)
    )
    next_revision = await prompt_revision(session, prompt_key) + 1
    if override is None:
        override = ReviewPromptOverride(
            prompt_key=prompt_key,
            instruction=text,
            revision=next_revision,
            updated_by=actor_id,
        )
        session.add(override)
    else:
        override.instruction = text
        override.revision = next_revision
        override.updated_by = actor_id
        override.updated_at = utc_now()
    await session.flush()
    await record_active_prompt_revision(
        session,
        prompt_key=prompt_key,
        instruction=text,
        source="owner",
        actor_id=actor_id,
        change_summary="The owner saved this production prompt.",
    )
    return override


async def clear_prompt_override(
    session: AsyncSession,
    *,
    prompt_key: str,
    actor_id: uuid.UUID,
) -> bool:
    await lock_review_prompt_catalog(session)
    override = await session.scalar(
        select(ReviewPromptOverride).where(ReviewPromptOverride.prompt_key == prompt_key)
    )
    if override is None:
        return False
    await session.delete(override)
    await session.flush()
    marker = await session.scalar(
        select(ReviewPromptOverride).where(ReviewPromptOverride.prompt_key == prompt_key).limit(1)
    )
    if marker is not None:  # pragma: no cover - defensive
        raise RuntimeError("prompt override was not removed")
    await record_active_prompt_revision(
        session,
        prompt_key=prompt_key,
        instruction=default_prompt_text(prompt_key),
        source="built_in",
        actor_id=actor_id,
        change_summary="The owner restored the built-in prompt.",
    )
    return True


async def start_review_reanalysis(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    actor_id: uuid.UUID,
    reason: str,
    scope: str = "analyzed",
    mode: str = "complete_review",
    start_stage: str = "source",
    reuse_policy: str = "none",
    publication_policy: str = "atomic",
    execution_prompt_version: str = "current",
    selection: dict[str, Any] | None = None,
) -> ReviewReanalysisRun:
    """Queue a fresh full review pass for photos that already carry a review."""

    locked_program = await session.scalar(
        select(LibraryProgram).where(LibraryProgram.id == program.id).with_for_update()
    )
    if locked_program is None:
        raise ValueError("library program is unavailable")
    program = locked_program

    if scope not in {"analyzed", "all", "photo", "selected", "rating", "version", "stale"}:
        raise ValueError("unsupported re-analysis scope")
    complete = (
        mode == "complete_review" and start_stage == "source" and publication_policy == "atomic"
    )
    single_stage = (
        mode == "step_only"
        and start_stage in {"primary", "qwen"}
        and publication_policy == "diagnostic"
    )
    if not (complete or single_stage) or reuse_policy != "none":
        raise ValueError(
            "advanced_reanalysis_disabled: bulk reanalysis supports a complete atomic review or "
            "one diagnostic Technical, Apple Vision, Primary, or Qwen stage"
        )
    stages = expanded_stages(mode=mode, start_stage=start_stage)
    requested_selection = dict(selection or {})
    active = await session.scalar(
        select(ReviewReanalysisRun)
        .where(
            ReviewReanalysisRun.program_id == program.id,
            ReviewReanalysisRun.state.in_({"running", "draining", "paused"}),
        )
        .order_by(ReviewReanalysisRun.created_at, ReviewReanalysisRun.id)
        .limit(1)
    )
    if active is not None:
        active_public_selection = {
            str(key): value
            for key, value in dict(active.selection or {}).items()
            if not str(key).startswith("_")
        }
        if (
            active.scope == scope
            and active_public_selection == requested_selection
            and active.mode == mode
            and active.start_stage == start_stage
            and active.reuse_policy == reuse_policy
            and active.publication_policy == publication_policy
            and active.reason == reason
        ):
            # A repeated button press or retried HTTP request returns the
            # already-durable owner action even if prompt maintenance happened
            # between requests. The active run's immutable prompt snapshot is
            # authoritative for that action.
            return active
        raise ValueError(
            "another re-analysis run is already active; resume, finish, or stop it first"
        )
    await ensure_prompt_revision_history(session, actor_id=actor_id)
    normalized_selection = requested_selection
    prompt_signature, prompt_revisions = await resolve_reanalysis_prompt_snapshot(
        session, execution_prompt_version
    )
    normalized_selection["_execution_prompt_version"] = prompt_signature
    normalized_selection["_execution_prompt_revisions"] = prompt_revisions
    normalized_selection["_execution_calibration_snapshot_id"] = (
        (program.model_pipeline_policy or {}).get("taste_calibration", {}).get("snapshot_id")
    )
    request_digest = canonical_digest(
        {
            "program_id": str(program.id),
            "scope": scope,
            "selection": normalized_selection,
            "mode": mode,
            "start_stage": start_stage,
            "reuse_policy": reuse_policy,
            "publication_policy": publication_policy,
            "reason": reason,
        }
    )
    program.review_generation += 1
    program.revision += 1
    run = ReviewReanalysisRun(
        program_id=program.id,
        review_generation=program.review_generation,
        scope=scope,
        selection=normalized_selection,
        request_digest=request_digest,
        mode=mode,
        start_stage=start_stage,
        expanded_stages=list(stages),
        reuse_policy=reuse_policy,
        publication_policy=publication_policy,
        reason=reason,
        state="running",
        requested_by=actor_id,
        selection_cutoff=utc_now(),
    )
    session.add(run)
    await session.flush()
    # Persist the immutable denominator before materialization starts.  The
    # LibraryProgram may describe a different intake run (including an old
    # one-photo repair), so it can never be used as re-analysis progress.
    frozen = _reanalysis_asset_statement(run).order_by(None).subquery()
    work_run = WorkRun(
        id=run.id,
        kind="reanalysis",
        program_id=program.id,
        library_generation=program.library_generation,
        requested=None,
        created_at=run.created_at,
    )
    session.add(work_run)
    await session.flush()
    await session.execute(
        insert(WorkRunAsset).from_select(
            ["run_id", "asset_id", "asset_revision_id"],
            select(literal(run.id, type_=Uuid()), frozen.c.id, frozen.c.current_revision_id),
        )
    )
    requested_photo_count = int(
        await session.scalar(
            select(func.count()).select_from(WorkRunAsset).where(WorkRunAsset.run_id == run.id)
        )
        or 0
    )
    work_run.requested = requested_photo_count
    if requested_photo_count == 0:
        raise ValueError("No photos matched the requested selection")
    from star.services.run_summary import reanalysis_task_type

    work_run.snapshot = {
        "task_type": reanalysis_task_type(run),
        "run_id": str(run.id),
        "kind": "reanalysis",
        "requested": requested_photo_count,
        "loaded": requested_photo_count,
        "not_loaded": 0,
        "source_shortfall": 0,
        "waiting": requested_photo_count,
        "reviewing": 0,
        "finished": 0,
        "excluded": 0,
        "remaining": requested_photo_count,
        "state": "running",
        "complete": False,
        "reconciled": True,
        "revision": 1,
    }
    work_run.observed_at = utc_now()
    run.selection = {
        **run.selection,
        "_requested_photo_count": requested_photo_count,
        "_membership_frozen": True,
    }
    await session.flush()
    return run


def _reanalysis_asset_statement(run: ReviewReanalysisRun) -> Any:
    statement = (
        select(Asset)
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .where(
            Asset.media_type == MediaType.IMAGE,
            Asset.access_state == "available",
            Asset.current_revision_id.is_not(None),
            AssetRevision.is_trashed.is_(False),
            Asset.created_at <= run.selection_cutoff,
        )
        .order_by(Asset.created_at, Asset.id)
    )
    if run.selection.get("_membership_frozen"):
        # Every run freezes its membership, so this is the branch every run
        # takes. It used to return here, before the cursor at the bottom was
        # applied, and no wave ever moved past its first chunk: the same two
        # hundred and fifty photographs were re-selected on every pass, given
        # fresh generations, and had the work behind them cancelled. A hundred
        # and fourteen thousand scans published two hundred and sixty-three.
        statement = statement.where(
            exists(
                select(WorkRunAsset.asset_id).where(
                    WorkRunAsset.run_id == run.id,
                    WorkRunAsset.asset_id == Asset.id,
                )
            )
        )
        return _after_cursor(statement, run)
    if run.scope == "run":
        statement = statement.where(
            Asset.id.in_(
                select(WorkRunAsset.asset_id).where(
                    WorkRunAsset.run_id == uuid.UUID(str(run.selection["run_id"]))
                )
            )
        )
    if run.scope == "analyzed":
        statement = statement.where(
            exists(
                select(Task.id).where(
                    Task.asset_id == Asset.id,
                    Task.task_type == "asset.primary_review",
                    Task.state == TaskState.SUCCEEDED,
                )
            )
        )
    elif run.scope == "rating":
        ratings = [
            value
            for value in run.selection.get("ratings", [])
            if isinstance(value, int) and 1 <= value <= 5
        ]
        statement = statement.join(
            AssetCurationSnapshot,
            AssetCurationSnapshot.asset_id == Asset.id,
        ).where(AssetCurationSnapshot.effective_rating.in_(ratings))
    elif run.scope == "stale":
        # Photographs whose current analysis was produced by a prompt, model
        # policy or calibration Star has since moved past. This is what makes an
        # improved prompt reach the whole library rather than only the
        # photographs reviewed after it shipped.
        statement = statement.join(
            AnalysisGeneration,
            AnalysisGeneration.id == Asset.current_generation_id,
        )
        signature = run.selection.get("current_signature") or {}
        drifted = []
        for field in ("prompt_version", "model_policy_version", "calibration_version"):
            expected = signature.get(field)
            if isinstance(expected, str) and expected:
                drifted.append(getattr(AnalysisGeneration, field) != expected)
        # Without a signature there is nothing to be stale against, and
        # selecting the whole library by accident would be far worse than
        # selecting nothing.
        statement = statement.where(or_(*drifted) if drifted else literal(False))
    elif run.scope == "version":
        statement = statement.join(
            AnalysisGeneration,
            AnalysisGeneration.id == Asset.current_generation_id,
        )
        releases = [
            value
            for value in run.selection.get("application_releases", [])
            if isinstance(value, str)
        ]
        rankers = run.selection.get("personal_ranker_versions", [])
        prompts = [
            value for value in run.selection.get("prompt_versions", []) if isinstance(value, str)
        ]
        if releases:
            statement = statement.where(AnalysisGeneration.application_release.in_(releases))
        if isinstance(rankers, list) and rankers:
            ranker_versions = [value for value in rankers if isinstance(value, int)]
            ranker_conditions = []
            if ranker_versions:
                ranker_conditions.append(
                    AnalysisGeneration.personal_ranker_version.in_(ranker_versions)
                )
            if "none" in rankers:
                ranker_conditions.append(AnalysisGeneration.personal_ranker_version.is_(None))
            statement = statement.where(or_(*ranker_conditions))
        if prompts:
            statement = statement.where(AnalysisGeneration.prompt_version.in_(prompts))
    selected_ids = run.selection.get("asset_ids")
    if run.scope in {"photo", "selected", "saved_filter"} and isinstance(selected_ids, list):
        normalized_ids: list[uuid.UUID] = []
        for value in selected_ids[:10_000]:
            try:
                normalized_ids.append(uuid.UUID(str(value)))
            except ValueError:
                continue
        statement = statement.where(Asset.id.in_(normalized_ids))
    elif run.scope in {"similarity_group", "burst"}:
        try:
            group_id = uuid.UUID(str(run.selection.get("group_id")))
        except ValueError:
            group_id = uuid.UUID(int=0)
        statement = statement.where(
            Asset.id.in_(
                select(CollectionGroupMember.asset_id).where(
                    CollectionGroupMember.group_id == group_id
                )
            )
        )
    elif run.scope == "processing_run":
        try:
            processing_batch_id = uuid.UUID(str(run.selection.get("processing_batch_id")))
        except ValueError:
            processing_batch_id = uuid.UUID(int=0)
        statement = statement.where(
            exists(
                select(Task.id).where(
                    Task.asset_id == Asset.id,
                    Task.processing_batch_id == processing_batch_id,
                )
            )
        )
    return _after_cursor(statement, run)


def _after_cursor(statement: Any, run: ReviewReanalysisRun) -> Any:
    """Keep only photographs past the run's cursor, in the order it walks them."""

    if run.cursor_created_at is not None:
        # The tiebreak must order exactly the way the query sorts, which is by
        # the identifier itself. Comparing a text cast of it against a text
        # identifier is a different ordering, and photographs imported in one
        # moment share a created_at to the microsecond: the whole selection
        # then rests on this tiebreak. When it disagreed with the sort, every
        # pass handed back the same chunk, created fresh generations for
        # photographs that already had one in flight, and cancelled the work
        # behind them. Twelve thousand scans published two hundred and ten.
        moved_on = [Asset.created_at > run.cursor_created_at]
        if run.cursor_asset_id is not None:
            moved_on.append(
                and_(
                    Asset.created_at == run.cursor_created_at,
                    Asset.id > run.cursor_asset_id,
                )
            )
        statement = statement.where(or_(*moved_on))
    return statement


async def _reanalysis_campaign_id(session: AsyncSession, asset: Asset) -> uuid.UUID | None:
    return await session.scalar(
        select(Task.campaign_id)
        .where(
            Task.asset_id == asset.id,
            Task.campaign_id.is_not(None),
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )


async def _reanalysis_task(
    session: AsyncSession,
    *,
    asset: Asset,
    campaign_id: uuid.UUID,
    run: ReviewReanalysisRun,
    generation_id: uuid.UUID,
) -> Task | None:
    manual_run_id = uuid.uuid5(
        uuid.UUID("2f4c8b1e-6f5a-4f2b-9c31-2b7a4c1d5e60"),
        f"{run.id}:{asset.id}",
    )
    common_ref = {
        "campaign_id": str(campaign_id),
        "asset_id": str(asset.id),
        "asset_revision_id": str(asset.current_revision_id),
        "manual_run_id": str(manual_run_id),
        "reanalysis_run_id": str(run.id),
        "review_generation": run.review_generation,
        "analysis_generation_id": str(generation_id),
        "reanalysis_expanded_stages": list(run.expanded_stages),
        "prompt_revisions": dict(run.selection.get("_execution_prompt_revisions", {})),
        "owner_calibration_snapshot_id": run.selection.get("_execution_calibration_snapshot_id"),
        "queue_origin": "user",
        "user_request_reason": run.reason,
    }

    if run.mode == "step_only" and run.start_stage in {"primary", "qwen"}:
        deep = run.start_stage == "qwen"
        models = await eligible_asset_vlm_models(
            session,
            limit=1,
            model_keys=["qwen3.8:27b"] if deep else None,
            runtimes=SENIOR_REVIEW_RUNTIMES if deep else None,
            capability_preference=None if deep else ("small-vlm",),
        )
        if not models:
            return None
        task = await enqueue_primary_review(
            session,
            asset=asset,
            campaign_id=campaign_id,
            model=models[0],
            deep_review=deep,
            escalation_reasons=["manual_second_opinion"] if deep else [],
            priority=REANALYSIS_PRIORITY,
            manual_request_id=manual_run_id,
            queue_origin="user",
            user_request_reason=f"manual_bulk_{run.start_stage}",
            analysis_generation_id=generation_id,
        )
        task.target_ref = {**task.target_ref, **common_ref}
        return task
    if run.mode == "step_only":
        return None

    # A full owner-requested review starts its authoritative model immediately.
    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        capability_preference=("small-vlm",),
    )
    if not models:
        return None
    primary = await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        model=models[0],
        priority=REANALYSIS_PRIORITY,
        manual_request_id=manual_run_id,
        queue_origin="user",
        user_request_reason="manual_bulk_complete_review",
        analysis_generation_id=generation_id,
    )
    primary.target_ref = {**primary.target_ref, **common_ref}
    return primary


async def ensure_stale_review_wave(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> ReviewReanalysisRun | None:
    """Start one re-review wave over photographs judged by an older contract.

    This is what keeps the library improving instead of finishing. When a prompt
    or a calibration changes, every photograph rated under the previous one is
    now judged by a standard Star no longer holds, and only a new pass can fix
    that. One wave runs at a time, it never starts while other work is queued,
    and it is keyed on the current contract so a finished wave does not restart
    until something actually changes.
    """

    if not program.enabled or program.state != LibraryProgramState.RUNNING:
        record_gate("stale_review_wave", refused="program_not_running")
        return None
    active = await session.scalar(
        select(ReviewReanalysisRun.id)
        .where(ReviewReanalysisRun.state.in_({"running", "draining"}))
        .limit(1)
    )
    if active is not None:
        record_gate("stale_review_wave", refused="run_active")
        return None
    # Never compete with the reviews the owner is waiting on. It waits for those
    # and not for an empty queue: media staging keeps a rolling runway topped up
    # by design, so a wave gated on every task type can never start at all, and
    # both machines sat idle for hours behind eighty staging jobs.
    pending = await session.scalar(
        select(Task.id)
        .where(
            Task.task_type.in_(REVIEW_TASK_TYPES),
            Task.state.in_(
                {
                    TaskState.PLANNED,
                    TaskState.QUEUED,
                    TaskState.RETRY_WAITING,
                    TaskState.RUNNING,
                }
            ),
        )
        .limit(1)
    )
    if pending is not None:
        record_gate("stale_review_wave", refused="queue_busy")
        return None
    signature = await current_analysis_signature(session)
    already = await session.scalar(
        select(ReviewReanalysisRun.id).where(
            ReviewReanalysisRun.scope == "stale",
            ReviewReanalysisRun.selection["current_signature"]["prompt_version"].as_string()
            == signature["prompt_version"],
            ReviewReanalysisRun.selection["current_signature"]["calibration_version"].as_string()
            == signature["calibration_version"],
        )
    )
    if already is not None:
        record_gate("stale_review_wave", refused="contract_already_covered")
        return None
    actor_id = await session.scalar(
        select(User.id).where(User.role == Role.ADMINISTRATOR, User.is_active.is_(True)).limit(1)
    )
    if actor_id is None:
        record_gate("stale_review_wave", refused="no_administrator")
        return None
    try:
        record_gate("stale_review_wave")
        return await start_review_reanalysis(
            session,
            program=program,
            actor_id=actor_id,
            reason="continuous_contract_refresh",
            scope="stale",
            selection={"current_signature": signature},
        )
    except ValueError:
        # "No photos matched" simply means the library is already current.
        record_gate("stale_review_wave", refused="nothing_stale")
        return None


async def materialize_next_review_reanalysis(session: AsyncSession) -> bool:
    """Queue one bounded chunk of the active re-analysis run."""

    run = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.state.in_({"running", "draining"}))
        .order_by(ReviewReanalysisRun.created_at, ReviewReanalysisRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is None:
        record_gate("materialize_reanalysis", refused="no_active_run")
        return False
    task_filter = reanalysis_task_filter(run.id)
    generation_ids = reanalysis_generation_ids(run.id)
    if run.publication_policy == "diagnostic":
        run.completed_task_count = int(
            await session.scalar(
                select(func.count(Task.id)).where(task_filter, Task.state == TaskState.SUCCEEDED)
            )
            or 0
        )
        run.failed_task_count = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    task_filter, Task.state.in_({TaskState.FAILED, TaskState.CANCELLED})
                )
            )
            or 0
        )
    else:
        run.completed_task_count = int(
            await session.scalar(
                select(func.count(AnalysisGeneration.id)).where(
                    AnalysisGeneration.id.in_(generation_ids),
                    (AnalysisGeneration.state == "published")
                    | AnalysisGeneration.published_at.is_not(None),
                )
            )
            or 0
        )
        run.failed_task_count = int(
            await session.scalar(
                select(func.count(AnalysisGeneration.id)).where(
                    AnalysisGeneration.id.in_(generation_ids),
                    AnalysisGeneration.state.in_({"failed", "cancelled", "abandoned"}),
                )
            )
            or 0
        )
        run.excluded_task_count = int(
            await session.scalar(
                select(func.count(AnalysisGeneration.id)).where(
                    AnalysisGeneration.id.in_(generation_ids),
                    AnalysisGeneration.state == "excluded",
                )
            )
            or 0
        )
    if run.publication_policy == "diagnostic":
        hot_count = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    task_filter,
                    Task.state.in_(
                        {
                            TaskState.PLANNED,
                            TaskState.QUEUED,
                            TaskState.RETRY_WAITING,
                            TaskState.RUNNING,
                            TaskState.CANCEL_REQUESTED,
                        }
                    ),
                )
            )
            or 0
        )
    else:
        # Count unfinished photos, not physical descendant tasks. Optional
        # evidence cannot inflate the runway and a Primary task that lost its
        # copied target metadata remains owned by the generation.
        hot_count = int(
            await session.scalar(
                select(func.count(AnalysisGeneration.id)).where(
                    AnalysisGeneration.id.in_(generation_ids),
                    AnalysisGeneration.state.in_({"building", "complete_unpublished"}),
                )
            )
            or 0
        )
    # A generation the library replaced is finished as far as this run is
    # concerned: it will never publish and nothing is still working on it.
    # Leaving it out of the denominator meant a run that had reached the end of
    # its selection could never reach a terminal state and drained forever.
    superseded_count = int(
        await session.scalar(
            select(func.count(AnalysisGeneration.id)).where(
                AnalysisGeneration.id.in_(generation_ids),
                AnalysisGeneration.state == "superseded",
                AnalysisGeneration.published_at.is_(None),
            )
        )
        or 0
    )
    terminal_count = (
        run.completed_task_count
        + run.failed_task_count
        + run.excluded_task_count
        + superseded_count
    )
    if run.state == "draining":
        if terminal_count >= run.planned_task_count:
            run.state = "failed" if run.failed_task_count else "complete"
            run.completed_at = utc_now()
            return True
        return False
    if hot_count >= REANALYSIS_HOT_LIMIT:
        record_gate("materialize_reanalysis", refused="hot_limit")
        return False
    assets = list(
        (await session.scalars(_reanalysis_asset_statement(run).limit(REANALYSIS_CHUNK_SIZE))).all()
    )
    if not assets:
        record_gate("materialize_reanalysis", refused="selection_exhausted")
        run.state = "draining"
        if terminal_count >= run.planned_task_count:
            run.state = "failed" if run.failed_task_count else "complete"
            run.completed_at = utc_now()
        return True
    record_gate("materialize_reanalysis")
    planned = 0
    for asset in assets:
        run.cursor_created_at = asset.created_at
        run.cursor_asset_id = asset.id
        run.scanned_count += 1
        campaign_id = await _reanalysis_campaign_id(session, asset)
        if campaign_id is None or asset.current_revision_id is None:
            run.skipped_count += 1
            continue
        generation = await create_analysis_generation(
            session,
            asset=asset,
            actor_id=run.requested_by,
            reason=run.reason,
            mode=run.mode,
            start_stage=run.start_stage,
            reuse_policy=run.reuse_policy,
            publication_policy=run.publication_policy,
            scope={"reanalysis_run_id": str(run.id), "scope": run.scope},
            prompt_revisions_override={
                str(key): int(value)
                for key, value in run.selection.get("_execution_prompt_revisions", {}).items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            },
            # A diagnostic single-stage check is append-only evidence. It must
            # not supersede an in-flight complete review for the same photo.
            supersede_active=run.mode != "step_only",
        )
        # The frozen membership row is what Home counts progress from, and it
        # only knows a photograph is finished through this link. Without it a
        # run reads "0 of 49,949" no matter how much has published.
        await session.execute(
            update(WorkRunAsset)
            .where(WorkRunAsset.run_id == run.id, WorkRunAsset.asset_id == asset.id)
            .values(generation_id=generation.id)
        )
        task = await _reanalysis_task(
            session,
            asset=asset,
            campaign_id=campaign_id,
            run=run,
            generation_id=generation.id,
        )
        if task is None:
            run.skipped_count += 1
            generation.state = "failed"
            generation.failure_class = "ReanalysisTaskUnavailable"
            generation.completed_at = utc_now()
            # This selected photo reached a terminal outcome and must remain in
            # the denominator; otherwise run progress can claim fewer photos
            # than the owner requested and conceal a permanent failure.
            planned += 1
            continue
        existing = await session.scalar(
            select(Task).where(Task.idempotency_key == task.idempotency_key)
        )
        if existing is not None and existing is not task:
            run.skipped_count += 1
            generation.state = "superseded"
            generation.failure_class = "DuplicateReanalysisMaterialization"
            generation.completed_at = utc_now()
            continue
        session.add(task)
        await session.flush()
        await bind_generation_task(
            session,
            generation_id=generation.id,
            stage=run.start_stage if run.mode == "step_only" else "primary",
            task=task,
        )
        planned += 1
    await session.flush()
    run.planned_task_count += planned
    return True


def reanalysis_summary(run: ReviewReanalysisRun) -> dict[str, Any]:
    requested = run.selection.get("_requested_photo_count")
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
        # Historical runs predate the immutable denominator. Keep their
        # progress truthful without inventing a total smaller than observed.
        requested = max(
            run.scanned_count,
            run.planned_task_count + run.skipped_count,
            run.completed_task_count + run.failed_task_count,
        )
    return {
        "id": str(run.id),
        "state": run.state,
        "scope": run.scope,
        "selection": {
            key: value for key, value in run.selection.items() if not str(key).startswith("_")
        },
        "mode": run.mode,
        "start_stage": run.start_stage,
        "expanded_stages": run.expanded_stages,
        "reuse_policy": run.reuse_policy,
        "publication_policy": run.publication_policy,
        "execution_prompt_version": str(
            run.selection.get("_execution_prompt_version") or "current"
        ),
        "reason": run.reason,
        "review_generation": run.review_generation,
        "requested": requested,
        "scanned": run.scanned_count,
        "queued": run.planned_task_count,
        "skipped": run.skipped_count,
        "excluded": run.excluded_task_count,
        "succeeded": run.completed_task_count,
        "failed": run.failed_task_count,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


__all__ = [
    "MAXIMUM_PROMPT_CHARACTERS",
    "MINIMUM_PROMPT_CHARACTERS",
    "PromptDefinition",
    "clear_prompt_override",
    "default_prompt_text",
    "effective_prompt_text",
    "materialize_next_review_reanalysis",
    "override_map",
    "prompt_bounds",
    "prompt_definition",
    "prompt_definitions",
    "prompt_revision",
    "prompt_text_at_revision",
    "reanalysis_summary",
    "resolve_reanalysis_prompt_snapshot",
    "save_prompt_override",
    "start_review_reanalysis",
    "validate_prompt_text",
]
