"""L01 origin search and L02 coordinate resolution; no recommendation calls."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_location_resolve_service, get_location_search_service
from app.schemas.common import ErrorResponse
from app.schemas.location import Coordinates
from app.schemas.locations import LocationResolveResponse, LocationSearchResponse, SearchQuery
from app.services.locations import LocationResolveService, LocationSearchService

router = APIRouter(prefix="/locations", tags=["locations"], route_class=ContractRoute)


@router.get(
    "/search",
    response_model=LocationSearchResponse,
    summary="출발지 장소명·주소 검색",
    description=(
        "보류 계약의 부분 구현입니다. 좌표로 지원 지역을 확인합니다. "
        "커서 미연결 상태에서는 다음 페이지가 필요한 검색에 503을 반환합니다."
    ),
    responses={code: {"model": ErrorResponse} for code in (422, 429, 500, 502, 503, 504)},
)
async def search(
    query: Annotated[
        SearchQuery, Query(description="검색할 장소명 또는 주소", examples=["서울역"])
    ],
    service: Annotated[LocationSearchService, Depends(get_location_search_service)],
    cursor: Annotated[str | None, Query(description="이전 응답의 불투명 검색 커서")] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
):
    return envelope(await service.search(query, cursor, limit))


@router.get(
    "/resolve",
    response_model=LocationResolveResponse,
    summary="좌표의 주소·지원 지역 확인",
    description=(
        "보류 계약의 구현입니다. 전달된 WGS84 좌표로 주소와 지원 지역을 확인합니다. "
        "지역 판별 실패를 지원 밖으로 간주하지 않으며 추천·수집은 실행하지 않습니다."
    ),
    responses={code: {"model": ErrorResponse} for code in (422, 429, 500, 502, 503, 504)},
)
async def resolve(
    latitude: Annotated[
        float,
        Query(ge=-90, le=90, allow_inf_nan=False, description="WGS84 위도", examples=[37.5665]),
    ],
    longitude: Annotated[
        float,
        Query(ge=-180, le=180, allow_inf_nan=False, description="WGS84 경도", examples=[126.978]),
    ],
    service: Annotated[LocationResolveService, Depends(get_location_resolve_service)],
):
    return envelope(await service.resolve(Coordinates(latitude=latitude, longitude=longitude)))
