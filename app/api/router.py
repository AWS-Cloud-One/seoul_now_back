from fastapi import APIRouter

from app.api.routes import health, locations, map, parking, pending, recommendations

router = APIRouter(prefix="/api/v1")
for module in (health, map, locations, parking, recommendations, pending):
    router.include_router(module.router)
