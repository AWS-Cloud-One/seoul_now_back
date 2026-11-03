from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.kakao import KakaoLocalClient
from app.core.config import Settings
from app.repositories.health import HealthRepository
from app.repositories.map import MapRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.services.health import HealthService
from app.services.locations import LocationResolveService, LocationSearchService
from app.services.map import MapService
from app.services.parking_detail import ParkingDetailService
from app.services.parking_nearby import ParkingNearbyService
from app.services.recommendations import RecommendationService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # One session per request. Closing it rolls back unfinished transactions.
    async with request.app.state.session_factory() as session:
        yield session


def get_health_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthService:
    repository = HealthRepository(session)
    return HealthService(repository, timeout_seconds=settings.health_timeout_seconds)


Session = Annotated[AsyncSession, Depends(get_session)]


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=10) as http:
        yield http


def get_kakao_local_client(
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KakaoLocalClient:
    return KakaoLocalClient(http, settings.kakao_rest_api_key.get_secret_value())


def get_parking_detail_service(session: Session) -> ParkingDetailService:
    return ParkingDetailService(ParkingDetailRepository(session))


def get_map_service(session: Session) -> MapService:
    return MapService(MapRepository(session))


def get_location_search_service(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> LocationSearchService:
    return LocationSearchService(provider)


def get_location_resolve_service(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> LocationResolveService:
    return LocationResolveService(provider)


def get_parking_nearby_service(
    session: Session,
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> ParkingNearbyService:
    return ParkingNearbyService(ParkingNearbyRepository(session), provider)


def get_recommendation_service() -> RecommendationService:
    """Connect the team's condition validator and recommendation engine here."""
    return RecommendationService()
