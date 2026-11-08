"""Part 1 extra query routes, mounted under the existing /locations router.

Public paths are integration proposals; no shared router/dependency edits needed.
"""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_http_client, get_kakao_local_client, get_session, get_settings
from app.clients.area_extras import SeoulAreaExtras
from app.clients.kakao import KakaoLocalClient
from app.clients.query_kakao import KakaoQueryClient
from app.core.config import Settings
from app.repositories.area_queries import AreaQueryRepository
from app.schemas.common import ErrorResponse
from app.schemas.location import Coordinates
from app.schemas.query_services import (
    AreaOverviewResponse,
    DrivingQuery,
    DrivingResponse,
    NearbyPlaceQuery,
    PlaceSearchQuery,
    PlaceSearchResponse,
)
from app.services.area_queries import AreaQueryService
from app.services.place_queries import PlaceQueryService

router = APIRouter(route_class=ContractRoute)
ERRORS = {code: {"model": ErrorResponse} for code in (404, 410, 422, 429, 500, 502, 503, 504)}


def get_place_query_service(provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)]):
    return PlaceQueryService(KakaoQueryClient(provider))


def get_area_query_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    return AreaQueryService(
        AreaQueryRepository(session),
        SeoulAreaExtras(
            http,
            settings.seoul_citydata_api_key.get_secret_value(),
        ),
    )


@router.get(
    "/routes/driving",
    response_model=DrivingResponse,
    summary="독립 자동차 경로 조회 — 파트 1 초안",
    responses=ERRORS,
)
async def driving(
    query: Annotated[DrivingQuery, Query()],
    service: Annotated[PlaceQueryService, Depends(get_place_query_service)],
):
    return envelope(
        await service.driving(
            Coordinates(latitude=query.origin_latitude, longitude=query.origin_longitude),
            Coordinates(latitude=query.destination_latitude, longitude=query.destination_longitude),
            include_geometry=query.include_geometry,
        )
    )


@router.get(
    "/places/search",
    response_model=PlaceSearchResponse,
    summary="도착지 자유 검색 — 파트 1 초안",
    responses=ERRORS,
)
async def places(
    query: Annotated[PlaceSearchQuery, Query()],
    service: Annotated[PlaceQueryService, Depends(get_place_query_service)],
):
    return envelope(await service.search(query.query, query.limit, query.cursor))


@router.get(
    "/places/nearby",
    response_model=PlaceSearchResponse,
    summary="주변 음식점·카페 검색 — 파트 1 초안",
    responses=ERRORS,
)
async def nearby_places(
    query: Annotated[NearbyPlaceQuery, Query()],
    service: Annotated[PlaceQueryService, Depends(get_place_query_service)],
):
    return envelope(
        await service.search(
            "음식점" if query.category == "food" else "카페",
            query.limit,
            query.cursor,
            center=Coordinates(latitude=query.latitude, longitude=query.longitude),
            radius=query.radius_meters,
            category="FD6" if query.category == "food" else "CE7",
        )
    )


@router.get(
    "/areas/{area_code}/overview",
    response_model=AreaOverviewResponse,
    summary="인구 관측 영역 현황 — 파트 1 초안",
    responses=ERRORS,
)
async def area_overview(
    area_code: str,
    service: Annotated[AreaQueryService, Depends(get_area_query_service)],
    include_extras: bool = False,
):
    return envelope(
        jsonable_encoder(await service.overview(area_code, include_extras=include_extras))
    )
