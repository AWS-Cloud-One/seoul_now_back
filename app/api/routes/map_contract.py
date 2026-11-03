"""M01/M02 HTTP boundary; spatial queries live in MapRepository."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_session
from app.repositories.map import MapRepository
from app.schemas.common import ErrorResponse
from app.schemas.map import GeometryResponse, Layer, MapQuery, StatusLayer, StatusResponse
from app.services.map_contract import MapService, parse_bbox

router = APIRouter(tags=["map-contract"], route_class=ContractRoute)


def get_map_query(
    bbox: Annotated[
        str,
        Query(description="WGS84 west,south,east,north", examples=["126.95,37.54,127.03,37.59"]),
    ],
    level: Annotated[int, Query(gt=0, description="Kakao map level", examples=[6])],
) -> MapQuery:
    return MapQuery(bbox=parse_bbox(bbox), level=level)


def get_map_service(session: Annotated[AsyncSession, Depends(get_session)]) -> MapService:
    return MapService(MapRepository(session))


@router.get(
    "/map/layers/{layer}/geometry",
    response_model=GeometryResponse,
    summary="지도 레이어 위치 조회",
    description=(
        "저장된 도로선·주차 위치·인구 관측 경계를 조회합니다. 외부 수집은 실행하지 않습니다."
    ),
    responses={code: {"model": ErrorResponse} for code in (422, 500, 503)},
)
async def geometry(
    layer: Layer,
    query: Annotated[MapQuery, Depends(get_map_query)],
    service: Annotated[MapService, Depends(get_map_service)],
):
    return envelope(await service.geometry(layer, query))


@router.get(
    "/map/layers/{layer}/status",
    response_model=StatusResponse,
    summary="도로·인구 레이어 상태 조회",
    description="M01과 같은 bbox·level·geometry_version으로 요청합니다. 기하는 반환하지 않습니다.",
    responses={code: {"model": ErrorResponse} for code in (409, 422, 500, 503)},
)
async def status(
    layer: StatusLayer,
    query: Annotated[MapQuery, Depends(get_map_query)],
    geometry_version: Annotated[str, Query(min_length=1, max_length=100)],
    service: Annotated[MapService, Depends(get_map_service)],
):
    return envelope(await service.status(layer, query, geometry_version))
