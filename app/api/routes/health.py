from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_health_service
from app.core.exceptions import DatabaseUnavailableError
from app.schemas.health import HealthResponse
from app.services.health import HealthService

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"description": "Database or PostGIS is unavailable"}},
)
async def readiness(
    service: Annotated[HealthService, Depends(get_health_service)],
) -> HealthResponse:
    try:
        await service.check_readiness()
    except DatabaseUnavailableError:
        # Do not expose database credentials or driver errors through the health API.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database or PostGIS is unavailable",
        ) from None
    return HealthResponse()
