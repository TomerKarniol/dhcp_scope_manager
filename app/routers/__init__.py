from fastapi import APIRouter

from app.routers.health import router as health_router
from app.routers.scopes import router as scopes_router

router = APIRouter()
router.include_router(scopes_router)
router.include_router(health_router)
