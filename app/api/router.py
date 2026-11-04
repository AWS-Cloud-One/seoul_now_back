from fastapi import APIRouter

from app.api.routes import health, locations, map, parking, recommendations

router = APIRouter(prefix="/api/v1")
for module in (health, map, locations, parking, recommendations):
    router.include_router(module.router)
