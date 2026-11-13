from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, exists, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal
from star.api.routers.assets._shared import (
    DestinationKey,
    GallerySort,
    _authoritative_sensitive_expression,
    _current_rating_votes,
    _decode_gallery_cursor,
    _encode_gallery_cursor,
    _gallery_destination_recommendations,
    _gallery_metadata,
    _rating_consensus,
    viewer,
)
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.models import (
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetRevision,
    CollectionGroup,
    CollectionGroupMember,
    ImmichConnection,
    LibraryProgram,
    PersonReference,
    Proposal,
    ReviewReanalysisRun,
    WorkRun,
    WorkRunAsset,
)
from star.db.session import get_session
from star.domain.enums import (
    MediaType,
    ProposalState,
    ProposalType,
)
from star.domain.schemas import (
    AssetGalleryFacetsResponse,
    AssetGalleryItemResponse,
    AssetGalleryPageResponse,
    CollectionBurstDetailResponse,
    CollectionBurstMemberResponse,
    SmartCollectionPreviewResponse,
    SmartCollectionResponse,
    SmartCollectionsResponse,
)
from star.services.asset_ratings import current_owner_rating_rows
from star.services.sensitive_content import (
    SENSITIVE_CONTENT_VALUES,
)
from star.services.visual_revision import visual_revision_key

router = APIRouter(tags=["Assets"])


@router.get("/gallery/facets", response_model=AssetGalleryFacetsResponse)
async def get_gallery_facets(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetGalleryFacetsResponse:
    captured = func.coalesce(
        AssetRevision.normalized_metadata["localDateTime"].as_string(),
        AssetRevision.normalized_metadata["fileCreatedAt"].as_string(),
        AssetRevision.normalized_metadata["dateTimeOriginal"].as_string(),
        "",
    )
    camera_make = func.coalesce(
        AssetRevision.normalized_metadata["exifInfo"]["make"].as_string(), ""
    )
    camera_model = func.coalesce(
        AssetRevision.normalized_metadata["exifInfo"]["model"].as_string(), ""
    )
    camera = func.trim(func.concat(camera_make, " ", camera_model))
    year = func.substr(captured, 1, 4)
    month = func.substr(captured, 1, 7)
    base = [
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        AssetRevision.is_trashed.is_(False),
    ]

    async def options(
        expression: Any,
        *,
        descending: bool = False,
        limit: int = 500,
    ) -> list[dict[str, str | int]]:
        count = func.count(func.distinct(Asset.id))
        rows = (
            await session.execute(
                select(expression, count)
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .where(*base, expression != "")
                .group_by(expression)
                .order_by(count.desc() if descending else expression.desc())
                .limit(limit)
            )
        ).all()
        return [
            {"value": str(value), "label": str(value), "count": int(total)}
            for value, total in rows
            if value
        ]

    visible_face = and_(
        AssetFaceReference.asset_id == Asset.id,
        AssetFaceReference.access_state == "available",
        AssetFaceReference.is_visible.is_(True),
    )
    total = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(*base)
        )
        or 0
    )
    favorite_count = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(*base, AssetRevision.is_favorite.is_(True))
        )
        or 0
    )
    with_faces_count = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(*base, exists(select(AssetFaceReference.id).where(visible_face)))
        )
        or 0
    )
    burst_count = int(
        await session.scalar(
            select(func.count(CollectionGroup.id)).where(
                CollectionGroup.group_type == "burst",
                CollectionGroup.state == "active",
            )
        )
        or 0
    )
    people_rows = (
        await session.execute(
            select(
                PersonReference.id,
                PersonReference.encrypted_display_name,
                func.count(func.distinct(AssetFaceReference.asset_id)),
            )
            .join(
                AssetFaceReference,
                and_(
                    AssetFaceReference.connection_id == PersonReference.connection_id,
                    AssetFaceReference.immich_person_id == PersonReference.immich_person_id,
                ),
            )
            .join(Asset, Asset.id == AssetFaceReference.asset_id)
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(
                *base,
                PersonReference.access_state == "available",
                PersonReference.is_hidden.is_(False),
                AssetFaceReference.access_state == "available",
                AssetFaceReference.is_visible.is_(True),
            )
            .group_by(PersonReference.id, PersonReference.encrypted_display_name)
            .order_by(func.count(func.distinct(AssetFaceReference.asset_id)).desc())
            .limit(250)
        )
    ).all()
    cipher = SecretCipher(settings.require_encryption_key())
    people = [
        {
            "value": str(person_id),
            "label": cipher.decrypt(encrypted_name),
            "count": int(count),
        }
        for person_id, encrypted_name, count in people_rows
    ]
    return AssetGalleryFacetsResponse(
        years=await options(year),
        months=await options(month),
        cameras=await options(camera, descending=True, limit=250),
        people=people,
        total=total,
        favorite_count=favorite_count,
        with_faces_count=with_faces_count,
        without_faces_count=max(0, total - with_faces_count),
        burst_count=burst_count,
    )


@router.get("/gallery", response_model=AssetGalleryPageResponse)
async def list_gallery_assets(
    asset_ids: Annotated[list[uuid.UUID] | None, Query(max_length=12)] = None,
    cursor: str | None = None,
    limit: int = Query(default=48, ge=12, le=500),
    connection_id: uuid.UUID | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=200),
    sort: GallerySort = "newest",
    rating_min: int | None = Query(default=None, ge=1, le=5),
    rating_max: int | None = Query(default=None, ge=1, le=5),
    agreement_max: float | None = Query(default=None, ge=0, le=1),
    vote_count_min: int | None = Query(default=None, ge=1, le=24),
    unrated: bool = False,
    analysis_state: Literal["all", "complete", "incomplete", "analyzed", "waiting"] = "all",
    destination: DestinationKey | None = None,
    destination_score_min: int | None = Query(default=None, ge=0, le=100),
    destination_status: Literal["waiting", "blocked", "low", "candidate", "recommended"]
    | None = None,
    sensitivity: Literal["flagged", "safe"] | None = None,
    year: str | None = Query(default=None, pattern=r"^\d{4}$"),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    camera: str | None = Query(default=None, min_length=1, max_length=200),
    person_id: uuid.UUID | None = None,
    faces: Literal["any", "named", "none"] | None = None,
    favorite: bool | None = None,
    human_rated: bool = False,
    rated_by_me: Literal["all", "yes", "no"] = "all",
    sequence: Literal["all", "bursts", "singles"] = "all",
    grouping: Literal["bursts", "individual"] = "bursts",
    semantic_query: str | None = Query(default=None, min_length=2, max_length=200),
    include_total: bool = True,
    current_job: bool = False,
    run_id: uuid.UUID | None = None,
    run_state: Literal["loaded", "waiting", "reviewing", "finished", "excluded", "unfinished"]
    | None = None,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetGalleryPageResponse:
    if rating_min is not None and rating_max is not None and rating_min > rating_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="minimum rating cannot exceed maximum rating",
        )
    # Gallery reads are deliberately read-only. Reviewed assets receive their
    # projection atomically at publication; older or unreviewed assets use the
    # safe revision/proposal fallback below. A 220k-photo browse must never turn
    # into a hidden projection-writing job.
    filename_expression = func.coalesce(
        AssetRevision.normalized_metadata["originalFileName"].as_string(), ""
    )
    captured_expression = func.coalesce(
        AssetRevision.normalized_metadata["localDateTime"].as_string(),
        AssetRevision.normalized_metadata["fileCreatedAt"].as_string(),
        "",
    )
    camera_make_expression = func.coalesce(
        AssetRevision.normalized_metadata["exifInfo"]["make"].as_string(), ""
    )
    camera_model_expression = func.coalesce(
        AssetRevision.normalized_metadata["exifInfo"]["model"].as_string(), ""
    )
    camera_expression = func.trim(func.concat(camera_make_expression, " ", camera_model_expression))
    owner_ratings = current_owner_rating_rows(session)
    owner_join = owner_ratings.c.asset_key == func.replace(sql_cast(Asset.id, String), "-", "")
    effective_rating_expression = func.coalesce(
        owner_ratings.c.rating,
        AssetCurationSnapshot.effective_rating,
        AssetRevision.rating,
        0,
    )
    if current_job and run_id is None:
        from star.services.run_summary import requested_run_id

        program = await session.scalar(select(LibraryProgram).limit(1))
        run_id = await requested_run_id(session, program) if program else None
    if run_state is not None and run_id is None:
        raise HTTPException(422, "Job progress filtering requires a run")
    conditions = [
        Asset.media_type == MediaType.IMAGE,
    ]
    if run_id is None:
        conditions.extend([Asset.access_state == "available", AssetRevision.is_trashed.is_(False)])
    if current_job and run_id is None:
        conditions.append(Asset.id.is_(None))
    if connection_id is not None:
        conditions.append(Asset.connection_id == connection_id)
    if run_id is not None:
        from star.services.run_summary import (
            legacy_member_condition,
            member_state_condition,
            reanalysis_task_type,
        )

        run = await session.get(WorkRun, run_id)
        if run is None:
            legacy = await session.get(ReviewReanalysisRun, run_id)
            if legacy is None:
                raise HTTPException(
                    503, "Preparing the job's photo list", headers={"Retry-After": "5"}
                )
            conditions.append(
                legacy_member_condition(
                    run_id,
                    run_state,
                    task_type=reanalysis_task_type(legacy),
                )
            )
            analysis_state = "all"
        membership = select(WorkRunAsset.asset_id).where(
            WorkRunAsset.run_id == run_id, WorkRunAsset.asset_id == Asset.id
        )
        if run is not None and run_state is not None and run_state != "loaded":
            membership = membership.where(
                member_state_condition(
                    run_state,
                    task_type=str(run.snapshot.get("task_type") or "asset.primary_review"),
                )
            )
            analysis_state = (
                "all"  # This filter describes the selected run, not a later global revision.
            )
        if run is not None:
            # A partial projection still has valid recorded members. Let owners
            # browse them while bounded reconciliation adds the remainder.
            conditions.append(exists(membership))
    if asset_ids is not None:
        conditions.append(Asset.id.in_(asset_ids))
    if rated_by_me == "yes" or human_rated:
        conditions.append(owner_ratings.c.rating.is_not(None))
    elif rated_by_me == "no":
        # The snapshot is outer-joined: coalesce keeps snapshot-less photos —
        # exactly the not-yet-reviewed ones this filter must include.
        conditions.append(owner_ratings.c.rating.is_(None))
    if query:
        search = query.strip()
        conditions.append(
            or_(
                filename_expression.icontains(search, autoescape=True),
                camera_make_expression.icontains(search, autoescape=True),
                camera_model_expression.icontains(search, autoescape=True),
                AssetCurationSnapshot.description.icontains(search, autoescape=True),
            )
        )
    if unrated:
        conditions.append(effective_rating_expression == 0)
    elif rating_min is not None:
        conditions.append(effective_rating_expression >= rating_min)
    if not unrated and rating_max is not None:
        conditions.append(effective_rating_expression <= rating_max)
    if agreement_max is not None:
        conditions.append(func.coalesce(AssetCurationSnapshot.agreement, 0) <= agreement_max)
    if vote_count_min is not None:
        conditions.append(func.coalesce(AssetCurationSnapshot.vote_count, 0) >= vote_count_min)
    has_analysis = exists(
        select(AnalysisRun.id).where(
            AnalysisRun.asset_id == Asset.id,
            AnalysisRun.analysis_type == "technical_preview",
            AnalysisRun.status == "succeeded",
        )
    )
    has_flagged_sensitive_content = _authoritative_sensitive_expression(Asset.id).in_(
        SENSITIVE_CONTENT_VALUES
    )
    if sensitivity == "flagged":
        conditions.append(has_flagged_sensitive_content)
    elif sensitivity == "safe":
        conditions.append(~has_flagged_sensitive_content)
    elif destination is not None:
        conditions.append(
            ~func.coalesce(_authoritative_sensitive_expression(Asset.id), "none").in_(
                SENSITIVE_CONTENT_VALUES
            )
        )
    if month is not None:
        conditions.append(func.substr(captured_expression, 1, 7) == month)
    elif year is not None:
        conditions.append(func.substr(captured_expression, 1, 4) == year)
    if camera is not None:
        conditions.append(camera_expression == camera)
    visible_face = and_(
        AssetFaceReference.asset_id == Asset.id,
        AssetFaceReference.access_state == "available",
        AssetFaceReference.is_visible.is_(True),
    )
    if person_id is not None:
        conditions.append(
            exists(
                select(AssetFaceReference.id)
                .join(
                    PersonReference,
                    and_(
                        PersonReference.connection_id == AssetFaceReference.connection_id,
                        PersonReference.immich_person_id == AssetFaceReference.immich_person_id,
                    ),
                )
                .where(visible_face, PersonReference.id == person_id)
            )
        )
    elif faces == "any":
        conditions.append(exists(select(AssetFaceReference.id).where(visible_face)))
    elif faces == "named":
        conditions.append(
            exists(
                select(AssetFaceReference.id).where(
                    visible_face,
                    AssetFaceReference.immich_person_id.is_not(None),
                )
            )
        )
    elif faces == "none":
        conditions.append(~exists(select(AssetFaceReference.id).where(visible_face)))
    if favorite is not None:
        conditions.append(AssetRevision.is_favorite.is_(favorite))
    if analysis_state == "complete":
        conditions.append(AssetCurationSnapshot.analysis_complete.is_(True))
    elif analysis_state == "incomplete":
        conditions.append(
            or_(
                AssetCurationSnapshot.asset_id.is_(None),
                AssetCurationSnapshot.analysis_complete.is_(False),
            )
        )
    elif analysis_state == "analyzed":
        conditions.append(has_analysis)
    elif analysis_state == "waiting":
        conditions.append(~has_analysis)
    active_burst_member = exists(
        select(CollectionGroupMember.id)
        .join(
            CollectionGroup,
            CollectionGroup.id == CollectionGroupMember.group_id,
        )
        .where(
            CollectionGroupMember.asset_id == Asset.id,
            CollectionGroup.group_type == "burst",
            CollectionGroup.state == "active",
        )
    )
    if sequence == "bursts":
        conditions.append(active_burst_member)
    elif sequence == "singles":
        conditions.append(~active_burst_member)
    if grouping == "bursts":
        hidden_burst_member = exists(
            select(CollectionGroupMember.id)
            .join(
                CollectionGroup,
                CollectionGroup.id == CollectionGroupMember.group_id,
            )
            .where(
                CollectionGroupMember.asset_id == Asset.id,
                CollectionGroup.group_type == "burst",
                CollectionGroup.state == "active",
                Asset.id
                != func.coalesce(
                    CollectionGroup.manual_representative_asset_id,
                    CollectionGroup.representative_asset_id,
                ),
            )
        )
        conditions.append(~hidden_burst_member)
    count_conditions = list(conditions)

    destination_score_expression = (
        func.coalesce(
            AssetCurationSnapshot.destination_recommendations[destination]["score"].as_integer(),
            -1,
        )
        if destination
        else func.coalesce(AssetCurationSnapshot.rank_score, -1)
    )
    if destination and destination_score_min is not None:
        conditions.append(destination_score_expression >= destination_score_min)
        count_conditions.append(destination_score_expression >= destination_score_min)
    if destination and destination_status is not None:
        status_expression = AssetCurationSnapshot.destination_recommendations[destination][
            "status"
        ].as_string()
        conditions.append(status_expression == destination_status)
        count_conditions.append(status_expression == destination_status)

    if semantic_query:
        # v209: the CLIP search index was retired. "Match by meaning" now runs
        # a plain text match over the model's stored description, summary and
        # tags, so the parameter keeps working without a vector index.
        search = semantic_query.strip()
        text_projection = or_(
            AssetCurationSnapshot.description.icontains(search, autoescape=True),
            AssetCurationSnapshot.final_summary.icontains(search, autoescape=True),
            sql_cast(AssetCurationSnapshot.content_tags, String).icontains(search, autoescape=True),
        )
        conditions.append(text_projection)
        count_conditions.append(text_projection)

    sort_config = {
        "review_priority": (func.coalesce(AssetCurationSnapshot.review_priority, 5000), True),
        "learned_desc": (func.coalesce(AssetCurationSnapshot.student_score, -1), True),
        "learned_asc": (func.coalesce(AssetCurationSnapshot.student_score, -1), False),
        "personal_desc": (func.coalesce(AssetCurationSnapshot.personal_score, -1), True),
        "personal_asc": (func.coalesce(AssetCurationSnapshot.personal_score, -1), False),
        "score_desc": (func.coalesce(AssetCurationSnapshot.rank_score, -1), True),
        "score_asc": (func.coalesce(AssetCurationSnapshot.rank_score, -1), False),
        "points_desc": (func.coalesce(AssetCurationSnapshot.library_points, -1), True),
        "points_asc": (func.coalesce(AssetCurationSnapshot.library_points, -1), False),
        "stars_desc": (effective_rating_expression, True),
        "stars_asc": (effective_rating_expression, False),
        "newest": (captured_expression, True),
        "oldest": (captured_expression, False),
        "filename": (func.lower(filename_expression), False),
        "destination_desc": (destination_score_expression, True),
    }
    sort_expression, descending = sort_config[sort]
    if cursor:
        cursor_value, cursor_id = _decode_gallery_cursor(cursor)
        comparison = (
            sort_expression < cursor_value if descending else sort_expression > cursor_value
        )
        conditions.append(
            or_(
                comparison,
                and_(
                    sort_expression == cursor_value,
                    Asset.id < cursor_id if descending else Asset.id > cursor_id,
                ),
            )
        )
    # Sort compact IDs first. Carrying the full JSON projections for every
    # matching photo through the sort needlessly loads the coordinator before LIMIT.
    page_candidates = (
        select(
            Asset.id.label("asset_id"),
            sort_expression.label("gallery_sort_value"),
            owner_ratings.c.rating.label("owner_rating"),
        )
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .join(ImmichConnection, ImmichConnection.id == Asset.connection_id)
        .outerjoin(owner_ratings, owner_join)
        .outerjoin(
            AssetCurationSnapshot,
            AssetCurationSnapshot.asset_id == Asset.id,
        )
        .where(*conditions)
        .order_by(
            sort_expression.desc() if descending else sort_expression.asc(),
            Asset.id.desc() if descending else Asset.id.asc(),
        )
        .limit(limit + 1)
        .subquery("gallery_page")
    )
    statement = (
        select(
            Asset,
            AssetRevision,
            ImmichConnection,
            AssetCurationSnapshot,
            page_candidates.c.gallery_sort_value,
            page_candidates.c.owner_rating,
        )
        .join(page_candidates, page_candidates.c.asset_id == Asset.id)
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .join(ImmichConnection, ImmichConnection.id == Asset.connection_id)
        .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
        .order_by(
            page_candidates.c.gallery_sort_value.desc()
            if descending
            else page_candidates.c.gallery_sort_value.asc(),
            Asset.id.desc() if descending else Asset.id.asc(),
        )
    )
    count_statement = (
        select(func.count(Asset.id))
        .outerjoin(owner_ratings, owner_join)
        .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
        .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
        .where(*count_conditions)
    )
    rows = list((await session.execute(statement)).all())
    total = int(await session.scalar(count_statement) or 0) if include_total else 0
    has_more = len(rows) > limit
    rows = rows[:limit]
    asset_ids = [row[0].id for row in rows]
    analyzed_assets: set[uuid.UUID] = set()
    proposal_rows: list[Proposal] = []
    if asset_ids:
        analyzed_assets = set(
            (
                await session.scalars(
                    select(AnalysisRun.asset_id)
                    .join(Asset, Asset.id == AnalysisRun.asset_id)
                    .where(
                        AnalysisRun.asset_id.in_(asset_ids),
                        or_(
                            Asset.current_generation_id.is_(None),
                            AnalysisRun.analysis_generation_id == Asset.current_generation_id,
                        ),
                        AnalysisRun.analysis_type == "technical_preview",
                        AnalysisRun.status == "succeeded",
                    )
                )
            ).all()
        )
        proposal_rows = list(
            (
                await session.scalars(
                    select(Proposal)
                    .join(Asset, Asset.id == Proposal.target_id)
                    .where(
                        Proposal.target_type == "asset",
                        Proposal.target_id.in_(asset_ids),
                        Proposal.proposal_type == ProposalType.RATING,
                        Proposal.state == ProposalState.PENDING,
                        or_(
                            Asset.current_generation_id.is_(None),
                            Proposal.analysis_generation_id == Asset.current_generation_id,
                        ),
                    )
                    .order_by(Proposal.created_at.desc())
                )
            ).all()
        )
    proposals_by_asset: dict[uuid.UUID, list[Proposal]] = {}
    for proposal in proposal_rows:
        proposals_by_asset.setdefault(proposal.target_id, []).append(proposal)
    bursts_by_representative: dict[uuid.UUID, CollectionGroup] = {}
    burst_stats: dict[uuid.UUID, tuple[int, int, int | None, int | None]] = {}
    if grouping == "bursts" and asset_ids:
        groups = list(
            (
                await session.scalars(
                    select(CollectionGroup).where(
                        CollectionGroup.group_type == "burst",
                        CollectionGroup.state == "active",
                        func.coalesce(
                            CollectionGroup.manual_representative_asset_id,
                            CollectionGroup.representative_asset_id,
                        ).in_(asset_ids),
                    )
                )
            ).all()
        )
        for group in groups:
            representative_id = (
                group.manual_representative_asset_id or group.representative_asset_id
            )
            if representative_id is not None:
                bursts_by_representative[representative_id] = group
        if groups:
            stats_rows = (
                await session.execute(
                    select(
                        CollectionGroupMember.group_id,
                        func.count(AssetCurationSnapshot.asset_id).filter(
                            AssetCurationSnapshot.analysis_complete.is_(True)
                        ),
                        func.count(CollectionGroupMember.asset_id).filter(
                            _authoritative_sensitive_expression(CollectionGroupMember.asset_id).in_(
                                SENSITIVE_CONTENT_VALUES
                            )
                        ),
                        func.min(AssetCurationSnapshot.effective_rating),
                        func.max(AssetCurationSnapshot.effective_rating),
                    )
                    .outerjoin(
                        AssetCurationSnapshot,
                        AssetCurationSnapshot.asset_id == CollectionGroupMember.asset_id,
                    )
                    .where(CollectionGroupMember.group_id.in_([group.id for group in groups]))
                    .group_by(CollectionGroupMember.group_id)
                )
            ).all()
            for group_id, reviewed, sensitive_count, minimum, maximum in stats_rows:
                burst_stats[group_id] = (
                    int(reviewed or 0),
                    int(sensitive_count or 0),
                    int(minimum) if minimum is not None else None,
                    int(maximum) if maximum is not None else None,
                )
    row_asset_ids = [row[0].id for row in rows]
    sensitive_by_asset = {
        asset_id: category
        for asset_id, category in (
            await session.execute(
                select(Asset.id, _authoritative_sensitive_expression(Asset.id)).where(
                    Asset.id.in_(row_asset_ids)
                )
            )
        ).all()
    }
    items: list[AssetGalleryItemResponse] = []
    for asset, revision, connection, snapshot, _sort_value, owner_rating in rows:
        filename, captured_at, camera = _gallery_metadata(revision.normalized_metadata)
        asset_proposals = proposals_by_asset.get(asset.id, [])
        consensus = _rating_consensus(_current_rating_votes(asset_proposals))
        sensitive_content = sensitive_by_asset.get(asset.id)
        burst = bursts_by_representative.get(asset.id)
        reviewed_count, sensitive_count, rating_minimum, rating_maximum = (
            burst_stats.get(burst.id, (0, 0, None, None))
            if burst is not None
            else (0, 0, None, None)
        )
        items.append(
            AssetGalleryItemResponse(
                id=asset.id,
                preview_version=visual_revision_key(revision),
                connection_id=asset.connection_id,
                source_account=connection.display_label,
                filename=filename,
                captured_at=captured_at,
                camera=camera,
                width=revision.width,
                height=revision.height,
                immich_rating=revision.rating,
                proposed_rating=consensus.rating if consensus else None,
                proposal_confidence=consensus.confidence if consensus else None,
                effective_rating=(
                    owner_rating
                    if owner_rating is not None
                    else snapshot.effective_rating
                    if snapshot
                    else revision.rating
                    if revision.rating is not None
                    else consensus.rating
                    if consensus
                    else None
                ),
                rating_source=(
                    "star_human"
                    if owner_rating is not None
                    else snapshot.rating_source
                    if snapshot
                    else "immich_human"
                    if revision.rating is not None
                    else "model_consensus"
                    if consensus
                    else "unrated"
                ),
                analysis_state=(
                    "complete"
                    if snapshot and snapshot.analysis_complete
                    else "partial"
                    if asset.id in analyzed_assets
                    else "waiting"
                ),
                is_favorite=revision.is_favorite,
                curation_score=snapshot.rank_score if snapshot else None,
                library_points=(snapshot.library_points or 0) if snapshot else 0,
                library_position=snapshot.library_position if snapshot else None,
                previous_library_points=snapshot.previous_library_points if snapshot else None,
                previous_library_position=(
                    snapshot.previous_library_position if snapshot else None
                ),
                preliminary_rating=snapshot.preliminary_rating if snapshot else None,
                ranking_confidence=snapshot.ranking_confidence if snapshot else 0,
                ranking_state=snapshot.ranking_state if snapshot else "unranked",
                ranking_version=snapshot.ranking_version if snapshot else 0,
                review_priority=snapshot.review_priority if snapshot else 5000,
                vote_count=snapshot.vote_count if snapshot else 0,
                agreement=snapshot.agreement if snapshot else 0,
                human_priority_points=snapshot.human_priority_points if snapshot else 0,
                recovery_outlook=snapshot.recovery_outlook if snapshot else "not_assessed",
                personal_score=snapshot.personal_score if snapshot else None,
                personal_confidence=snapshot.personal_confidence if snapshot else 0,
                personal_model_version_id=(
                    snapshot.personal_model_version_id if snapshot else None
                ),
                student_score=snapshot.student_score if snapshot else None,
                student_confidence=snapshot.student_confidence if snapshot else 0,
                student_model_version_id=(snapshot.student_model_version_id if snapshot else None),
                analysis_complete=snapshot.analysis_complete if snapshot else False,
                completed_checks=snapshot.completed_checks if snapshot else [],
                evidence_plan=snapshot.evidence_plan if snapshot else [],
                assessment_version=(
                    snapshot.assessment_version if snapshot else "asset-assessment-v3"
                ),
                destination_recommendations=(
                    _gallery_destination_recommendations(
                        snapshot.destination_recommendations,
                        assessment_version=snapshot.assessment_version,
                    )
                    if snapshot
                    else {}
                ),
                content_tags=snapshot.content_tags if snapshot else [],
                sensitive_content=sensitive_content,
                censored=(sensitive_content in SENSITIVE_CONTENT_VALUES or sensitive_count > 0),
                entry_kind="burst" if burst is not None else "photo",
                burst_id=burst.id if burst is not None else None,
                burst_count=burst.member_count if burst is not None else 1,
                burst_confidence=burst.confidence if burst is not None else None,
                burst_reviewed_count=reviewed_count,
                burst_sensitive_count=sensitive_count,
                burst_rating_min=rating_minimum,
                burst_rating_max=rating_maximum,
            )
        )
    next_cursor = None
    if has_more and rows:
        last_asset, _revision, _connection, _snapshot, last_sort_value, _owner_rating = rows[-1]
        next_cursor = _encode_gallery_cursor(last_sort_value, last_asset.id)
    return AssetGalleryPageResponse(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        total=total,
        ranking_population_count=max(
            int((await session.scalar(select(LibraryProgram.source_total_count).limit(1))) or 0),
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(Asset)
                    .where(
                        Asset.media_type == MediaType.IMAGE,
                        Asset.access_state == "available",
                    )
                )
                or 0
            ),
        )
        if include_total
        else 0,
    )


@router.get(
    "/bursts/{group_id}",
    response_model=CollectionBurstDetailResponse,
)
async def get_collection_burst(
    group_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> CollectionBurstDetailResponse:
    group = await session.scalar(
        select(CollectionGroup).where(
            CollectionGroup.id == group_id,
            CollectionGroup.group_type == "burst",
            CollectionGroup.state == "active",
        )
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="burst not found")
    representative_id = group.manual_representative_asset_id or group.representative_asset_id
    rows = list(
        (
            await session.execute(
                select(
                    CollectionGroupMember,
                    Asset,
                    AssetRevision,
                    AssetCurationSnapshot,
                )
                .join(Asset, Asset.id == CollectionGroupMember.asset_id)
                .join(
                    AssetRevision,
                    AssetRevision.id == CollectionGroupMember.asset_revision_id,
                )
                .outerjoin(
                    AssetCurationSnapshot,
                    AssetCurationSnapshot.asset_id == Asset.id,
                )
                .where(CollectionGroupMember.group_id == group.id)
                .order_by(CollectionGroupMember.sequence_index)
            )
        ).all()
    )
    asset_ids = [asset.id for _member, asset, _revision, _snapshot in rows]
    sensitive_assets: set[uuid.UUID] = set()
    if asset_ids:
        sensitive_assets = set(
            (
                await session.scalars(
                    select(Asset.id).where(
                        Asset.id.in_(asset_ids),
                        _authoritative_sensitive_expression(Asset.id).in_(SENSITIVE_CONTENT_VALUES),
                    )
                )
            ).all()
        )
    members: list[CollectionBurstMemberResponse] = []
    for member, asset, revision, snapshot in rows:
        filename, captured_at, _camera = _gallery_metadata(revision.normalized_metadata)
        members.append(
            CollectionBurstMemberResponse(
                asset_id=asset.id,
                preview_version=visual_revision_key(revision),
                filename=filename,
                captured_at=captured_at,
                sequence_index=member.sequence_index,
                effective_rating=(
                    snapshot.effective_rating if snapshot is not None else revision.rating
                ),
                analysis_complete=bool(snapshot is not None and snapshot.analysis_complete),
                is_representative=asset.id == representative_id,
                censored=asset.id in sensitive_assets,
            )
        )
    return CollectionBurstDetailResponse(
        id=group.id,
        confidence=group.confidence,
        explanation=group.explanation,
        member_count=group.member_count,
        representative_asset_id=representative_id,
        captured_start_at=group.captured_start_at,
        captured_end_at=group.captured_end_at,
        duration_ms=int(group.source_facts.get("duration_ms") or 0),
        maximum_gap_ms=int(group.source_facts.get("maximum_gap_ms") or 0),
        members=members,
    )


@router.get("/collections", response_model=SmartCollectionsResponse)
async def list_smart_collections(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> SmartCollectionsResponse:
    definitions = (
        (
            "highlights",
            "Highlights",
            "Automatic and human-confirmed 5-star photos.",
            None,
            5,
        ),
        (
            "keepers",
            "Keepers",
            "Strong 4-star photos outside the final highlight set.",
            None,
            4,
        ),
        (
            "rating_conflicts",
            "Rating conflicts",
            "Primary review and Qwen differ by two or more stars.",
            None,
            0,
        ),
        ("edit", "Edit next", "Useful non-destructive edits.", "edit", 50),
        (
            "print",
            "Print candidates",
            "Strong high-resolution print options.",
            "print",
            72,
        ),
        (
            "tv",
            "TV rotation",
            "Strong display photos.",
            "tv",
            72,
        ),
        (
            "website",
            "Sharing candidates",
            "Photos suitable for private sharing.",
            "website",
            72,
        ),
        (
            "private",
            "Sensitive / private",
            "Photos kept in the private workflow.",
            "private",
            50,
        ),
        (
            "cull_review",
            "Cull review",
            "Conservative low-value review candidates.",
            "cull_review",
            72,
        ),
    )
    base_conditions = (
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        AssetRevision.is_trashed.is_(False),
    )
    non_sensitive_content = ~func.coalesce(
        _authoritative_sensitive_expression(Asset.id), "none"
    ).in_(SENSITIVE_CONTENT_VALUES)
    items: list[SmartCollectionResponse] = []
    for key, label, description, destination, minimum_score in definitions:
        extra_conditions: tuple[Any, ...]
        if key in {"highlights", "keepers"}:
            collection_score = AssetCurationSnapshot.effective_rating
            score_condition = collection_score == minimum_score
            extra_conditions = (AssetCurationSnapshot.analysis_complete.is_(True),)
        elif key == "rating_conflicts":
            collection_score = AssetCurationSnapshot.rank_score
            score_condition = exists().where(
                Proposal.target_type == "asset",
                Proposal.target_id == Asset.id,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
                Proposal.value["rating_discrepancy"].as_integer() >= 2,
            )
            extra_conditions = (AssetCurationSnapshot.analysis_complete.is_(True),)
        elif destination is None:
            collection_score = AssetCurationSnapshot.rank_score
            score_condition = collection_score >= minimum_score
            extra_conditions = (AssetCurationSnapshot.analysis_complete.is_(True),)
        else:
            collection_score = AssetCurationSnapshot.destination_recommendations[destination][
                "score"
            ].as_integer()
            score_condition = collection_score >= minimum_score
            extra_conditions = (
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.destination_recommendations[destination]["status"]
                .as_string()
                .in_(["candidate", "recommended"]),
            )
        total = int(
            await session.scalar(
                select(func.count(Asset.id))
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
                .where(
                    *base_conditions,
                    non_sensitive_content,
                    score_condition,
                    *extra_conditions,
                )
            )
            or 0
        )
        preview_rows = list(
            (
                await session.execute(
                    select(Asset.id, AssetRevision)
                    .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                    .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
                    .where(
                        *base_conditions,
                        non_sensitive_content,
                        score_condition,
                        *extra_conditions,
                    )
                    .order_by(collection_score.desc(), Asset.id.desc())
                    .limit(7)
                )
            ).all()
        )
        items.append(
            SmartCollectionResponse(
                key=key,
                label=label,
                description=description,
                destination=destination,
                minimum_score=None if destination is None else minimum_score,
                total=total,
                preview_assets=[
                    SmartCollectionPreviewResponse(
                        id=asset_id,
                        preview_version=visual_revision_key(revision),
                    )
                    for asset_id, revision in preview_rows
                ],
            )
        )
    flagged_sensitive_content = _authoritative_sensitive_expression(Asset.id).in_(
        SENSITIVE_CONTENT_VALUES
    )
    sensitive_total = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(*base_conditions, flagged_sensitive_content)
        )
        or 0
    )
    items.insert(
        5,
        SmartCollectionResponse(
            key="nude",
            label="NSFW",
            description=(
                "Locally flagged intimate, sexually explicit, or suggestive photos. "
                "Previews stay censored "
                "until you deliberately open a photo."
            ),
            destination=None,
            minimum_score=None,
            total=sensitive_total,
            sensitive_preview=True,
        ),
    )
    indexed_total = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .where(*base_conditions)
        )
        or 0
    )
    completed_total = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
            .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(*base_conditions, AssetCurationSnapshot.analysis_complete.is_(True))
        )
        or 0
    )
    return SmartCollectionsResponse(
        items=items,
        indexed_total=indexed_total,
        completed_total=completed_total,
    )
