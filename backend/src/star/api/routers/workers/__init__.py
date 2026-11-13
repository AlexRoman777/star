from __future__ import annotations

from fastapi import APIRouter

from star.api.routers.workers import completion, enrollment, leasing, management, slots

router = APIRouter(tags=["Workers"])
router.include_router(management.router)
router.include_router(enrollment.router)
router.include_router(leasing.router)
router.include_router(completion.router)
router.include_router(slots.router)
