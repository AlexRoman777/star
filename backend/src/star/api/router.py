from __future__ import annotations

from fastapi import APIRouter

from star.api.routers import (
    analysis_audit,
    assets,
    audit,
    audit_access,
    auth,
    dashboard,
    deep_review,
    diagnostics,
    family_roster,
    health,
    highlights,
    immich,
    immich_library,
    library,
    library_reset,
    library_review,
    models,
    operations,
    owner_rating_sessions,
    performance,
    pipeline,
    processing,
    reanalysis,
    review_prompts,
    runs,
    system,
    ui,
    workers,
    writeback,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(runs.router)
api_router.include_router(performance.router)
api_router.include_router(health.router)
api_router.include_router(analysis_audit.router)
api_router.include_router(audit_access.router)
api_router.include_router(highlights.router)
api_router.include_router(library_review.router)
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(diagnostics.router)
api_router.include_router(deep_review.router)
api_router.include_router(family_roster.router)
api_router.include_router(review_prompts.router)
api_router.include_router(reanalysis.router)
api_router.include_router(assets.router)
api_router.include_router(immich.router)
api_router.include_router(immich_library.router)
api_router.include_router(library.router)
api_router.include_router(library_reset.router)
api_router.include_router(workers.router)
api_router.include_router(processing.router)
api_router.include_router(models.router)
api_router.include_router(operations.router)
api_router.include_router(owner_rating_sessions.router)
api_router.include_router(pipeline.router)
api_router.include_router(system.router)
api_router.include_router(ui.router)
api_router.include_router(writeback.router)
api_router.include_router(audit.router)
