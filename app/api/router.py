from fastapi import APIRouter

from app.api.routes import citydata, health, parking, traffic

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(parking.router)
router.include_router(citydata.router)

router.include_router(traffic.router)
