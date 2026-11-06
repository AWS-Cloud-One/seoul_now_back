"""P02/P04 product contracts using API_SPEC coordinate and response models."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_parking_detail_service, get_parking_nearby_service
from app.schemas.common import ErrorResponse
from app.schemas.location import Coordinates
from app.schemas.parking_detail import ParkingDetailResponse, ParkingErrorResponse
from app.schemas.parking_nearby import NearbyParkingQuery, NearbyParkingResponse
from app.services.parking_detail import ParkingDetailService
from app.services.parking_nearby import ParkingNearbyService

router = APIRouter(prefix="/parking", tags=["parking"], route_class=ContractRoute)


@router.get(
    "/nearby",
    response_model=NearbyParkingResponse,
    summary="주변 주차 등록정보 조회",
    description="P02 부분 구현. 다음 페이지 처리는 담당 기능 연결 전까지 503으로 응답합니다.",
    responses={code: {"model": ErrorResponse} for code in (422, 429, 500, 502, 503, 504)},
)
async def nearby(
    query: Annotated[NearbyParkingQuery, Query()],
    service: Annotated[ParkingNearbyService, Depends(get_parking_nearby_service)],
):
    return envelope(
        await service.search(
            Coordinates(latitude=query.latitude, longitude=query.longitude),
            query.limit,
            query.cursor,
        )
    )


# Static paths are registered before the UUID detail route.
@router.get(
    "/{parking_id}",
    response_model=ParkingDetailResponse,
    summary="주차장 등록 상세 조회",
    responses={
        404: {"model": ParkingErrorResponse, "description": "주차장 없음 또는 제공 종료"},
        422: {"model": ParkingErrorResponse, "description": "잘못된 UUID"},
        503: {"model": ParkingErrorResponse, "description": "등록정보 조회 불가"},
    },
)
async def parking_detail(
    parking_id: UUID,
    service: Annotated[ParkingDetailService, Depends(get_parking_detail_service)],
):
    return envelope(await service.get_detail(parking_id))
