"""Owner-editable review prompt text and full-library re-analysis."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.db.models import (
    ReviewPromptRevision,
    ReviewReanalysisRun,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    ReviewPromptCatalogResponse,
    ReviewPromptRevisionResponse,
    ReviewPromptTextResponse,
    ReviewPromptTextUpdate,
    ReviewReanalysisResponse,
    TasteCalibrationResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.campaigns import canonical_digest
from star.services.library_program import get_or_create_library_program
from star.services.operations import OperationRecord, append_operation_event
from star.services.review_prompts import (
    MAXIMUM_PROMPT_CHARACTERS,
    MINIMUM_PROMPT_CHARACTERS,
    activate_prompt_revision,
    clear_prompt_override,
    default_prompt_text,
    ensure_prompt_revision_history,
    override_map,
    prompt_bounds,
    prompt_definition,
    prompt_definitions,
    prompt_revision,
    prompt_revision_history,
    reanalysis_summary,
    reject_prompt_revision,
    save_prompt_override,
    start_review_reanalysis,
)
from star.services.taste_calibration import taste_calibration_summary

router = APIRouter(prefix="/review-prompts", tags=["Review Prompts"])
owner = require_roles(Role.ADMINISTRATOR)


async def _latest_reanalysis(session: AsyncSession) -> ReviewReanalysisRun | None:
    run: ReviewReanalysisRun | None = await session.scalar(
        select(ReviewReanalysisRun)
        .order_by(ReviewReanalysisRun.created_at.desc(), ReviewReanalysisRun.id.desc())
        .limit(1)
    )
    return run


async def _catalog(session: AsyncSession, *, actor_id: uuid.UUID) -> ReviewPromptCatalogResponse:
    program = await get_or_create_library_program(session, actor_id=actor_id)
    await ensure_prompt_revision_history(session, actor_id=actor_id)
    overrides = await override_map(session)
    active_deep_key = f"photo_deep_review:{program.active_deep_review_prompt}"
    active_primary_key = "primary_review"
    items: list[ReviewPromptTextResponse] = []
    for definition in prompt_definitions():
        override = overrides.get(definition.key)
        default_text = default_prompt_text(definition.key)
        minimum_characters, maximum_characters = prompt_bounds(definition.key)
        items.append(
            ReviewPromptTextResponse(
                prompt_key=definition.key,
                label=definition.label,
                machine=definition.machine,
                stage=definition.stage,
                description=definition.description,
                active=definition.key in {active_deep_key, active_primary_key}
                or definition.stage in {"rating_scale", "system_role"},
                overridden=override is not None,
                instruction=override.instruction if override else default_text,
                default_instruction=default_text,
                minimum_characters=minimum_characters,
                maximum_characters=maximum_characters,
                updated_at=override.updated_at if override else None,
            )
        )
    reanalysis = await _latest_reanalysis(session)
    calibration = taste_calibration_summary(program)
    return ReviewPromptCatalogResponse(
        revision=await prompt_revision(session),
        taste_calibration=(
            TasteCalibrationResponse(
                text=str(calibration.get("text") or ""),
                label_count=int(calibration.get("label_count") or 0),
                exact_agreement=(
                    float(calibration["exact_agreement"])
                    if isinstance(calibration.get("exact_agreement"), (int, float))
                    else None
                ),
                generated_at=(
                    str(calibration["generated_at"])
                    if isinstance(calibration.get("generated_at"), str)
                    else None
                ),
                mean_model_by_owner_star={
                    str(key): float(value)
                    for key, value in dict(
                        calibration.get("mean_model_by_owner_star") or {}
                    ).items()
                    if isinstance(value, (int, float))
                },
            )
            if calibration is not None
            else None
        ),
        minimum_characters=MINIMUM_PROMPT_CHARACTERS,
        maximum_characters=MAXIMUM_PROMPT_CHARACTERS,
        active_prompt_version=program.active_deep_review_prompt,
        review_generation=program.review_generation,
        items=items,
        revisions=[
            ReviewPromptRevisionResponse.model_validate(item)
            for item in await prompt_revision_history(session)
        ],
        reanalysis=(
            ReviewReanalysisResponse(**reanalysis_summary(reanalysis))
            if reanalysis is not None
            else None
        ),
    )


@router.get("", response_model=ReviewPromptCatalogResponse)
async def list_review_prompts(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ReviewPromptCatalogResponse:
    catalog = await _catalog(session, actor_id=principal.user.id)
    await session.commit()
    return catalog


@router.post(
    "/revisions/{revision_id}/{action}",
    response_model=ReviewPromptCatalogResponse,
)
async def change_prompt_revision_state(
    revision_id: uuid.UUID,
    action: str,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> ReviewPromptCatalogResponse:
    revision = await session.get(ReviewPromptRevision, revision_id)
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="prompt version not found"
        )
    try:
        if action == "activate":
            await activate_prompt_revision(
                session,
                revision=revision,
                actor_id=principal.user.id,
            )
        elif action == "reject":
            await reject_prompt_revision(session, revision=revision)
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown action")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type=f"review.prompt.revision.{action}",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={
                "prompt_key": revision.prompt_key,
                "prompt_revision_id": str(revision.id),
            },
            correlation_id=correlation_id,
            after={
                "version": revision.version,
                "source": revision.source,
                "status": revision.status,
                "instruction_digest": revision.instruction_digest,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type=f"review.prompt.revision.{action}",
            message=(
                "A saved prompt version became active"
                if action == "activate"
                else "A StarML prompt suggestion was rejected"
            ),
            correlation_id=correlation_id,
            details={"prompt_key": revision.prompt_key, "version": revision.version},
        ),
    )
    catalog = await _catalog(session, actor_id=principal.user.id)
    await session.commit()
    return catalog


@router.put("/{prompt_key:path}", response_model=ReviewPromptCatalogResponse)
async def put_review_prompt(
    prompt_key: str,
    payload: ReviewPromptTextUpdate,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> ReviewPromptCatalogResponse:
    if prompt_definition(prompt_key) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this prompt cannot be edited"
        )
    # Prompt activation and review-generation creation both touch the program
    # and prompt catalog. Always take the program lock first so a simultaneous
    # explicit re-review cannot deadlock with "save and re-review".
    program = await get_or_create_library_program(
        session,
        actor_id=principal.user.id,
        for_update=True,
    )
    try:
        override = await save_prompt_override(
            session,
            prompt_key=prompt_key,
            instruction=payload.instruction,
            actor_id=principal.user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    reanalysis = None
    if payload.reanalyze_completed_photos:
        reanalysis = await start_review_reanalysis(
            session,
            program=program,
            actor_id=principal.user.id,
            reason=f"prompt_change:{prompt_key}",
        )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="review.prompt.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"prompt_key": prompt_key},
            correlation_id=correlation_id,
            after={
                "prompt_revision": override.revision,
                "characters": len(override.instruction),
                "instruction_digest": canonical_digest(override.instruction),
                "reanalysis_id": str(reanalysis.id) if reanalysis is not None else None,
                "proposal_only": True,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="review.prompt.updated",
            message="A review prompt was replaced by the owner",
            correlation_id=correlation_id,
            details={
                "prompt_key": prompt_key,
                "prompt_revision": override.revision,
                "reanalysis_queued": reanalysis is not None,
            },
        ),
    )
    catalog = await _catalog(session, actor_id=principal.user.id)
    await session.commit()
    return catalog


@router.post("/reset/{prompt_key:path}", response_model=ReviewPromptCatalogResponse)
async def reset_review_prompt(
    prompt_key: str,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> ReviewPromptCatalogResponse:
    if prompt_definition(prompt_key) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this prompt cannot be edited"
        )
    removed = await clear_prompt_override(
        session, prompt_key=prompt_key, actor_id=principal.user.id
    )
    if removed:
        await append_audit_event(
            session,
            AuditRecord(
                event_type="review.prompt.reset",
                actor_type="user",
                actor_id=str(principal.user.id),
                target_ref={"prompt_key": prompt_key},
                correlation_id=correlation_id,
                after={"restored": "built_in_default"},
            ),
        )
    catalog = await _catalog(session, actor_id=principal.user.id)
    await session.commit()
    return catalog
