from __future__ import annotations

from fastapi import APIRouter

from star.api.routers.assets import detail, gallery, media, reanalysis

router = APIRouter(prefix="/assets", tags=["Assets"])
router.include_router(gallery.router)
router.include_router(reanalysis.router)
router.include_router(detail.router)
router.include_router(media.router)
