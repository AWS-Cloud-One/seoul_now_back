from fastapi import APIRouter

from app.api.routes import health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
