from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.contract import ContractError, ContractRoute, envelope
from app.api.dependencies import get_session
from app.api.routes.locations import get_kakao_local_client
from app.clients.kakao import KakaoLocalClient
from app.models.parking import CollectionRun
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.repositories.parking import ParkingRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.schemas.common import ErrorResponse
from app.schemas.location import Coordinates
from app.schemas.parking_detail import ParkingDetailResponse, ParkingErrorResponse
from app.schemas.parking_nearby import NearbyParkingResponse
from app.services.parking_detail import ParkingDetailService
from app.services.parking_nearby import ParkingNearbyService

router = APIRouter(prefix="/parking", tags=["parking"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/inspect", response_class=HTMLResponse, include_in_schema=False)
def inspect_parking() -> str:
    """Local data inspection utility, not the team's product frontend."""
    return (
        Path(__file__).resolve().parents[2] / "modules/data_pipeline/inspection.html"
    ).read_text(encoding="utf-8")


async def nearby(
    session: Session,
    lat: Annotated[float, Query(ge=37.4, le=37.75)],
    lon: Annotated[float, Query(ge=126.7, le=127.25)],
    source: Literal[
        "seoul_getparkinfo", "seoul_citydata_prk", "seoul_municipal_parking", "demo_parking"
    ] = SOURCE,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """Search nearby parking; expose only fresh, provider-confirmed vacancies."""
    return await ParkingRepository(session).nearby(lat, lon, source, limit)


def get_parking_nearby_service(
    session: Session,
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> ParkingNearbyService:
    return ParkingNearbyService(ParkingNearbyRepository(session), provider)


def is_contract_query(request: Request) -> bool:
    return any(key in request.query_params for key in ("latitude", "longitude", "cursor"))


class NearbyRoute(ContractRoute):
    def get_route_handler(self):
        contract = super().get_route_handler()
        legacy = APIRoute.get_route_handler(self)

        async def handler(request: Request):
            # Existing frontends retain their success and validation-error format.
            return await (contract(request) if is_contract_query(request) else legacy(request))

        return handler


nearby_router = APIRouter(route_class=NearbyRoute)


@nearby_router.get(
    "/nearby",
    response_model=None,
    summary="주변 주차 등록정보 조회",
    description=(
        "P02 보류 계약의 부분 구현. latitude·longitude는 새 계약, lat·lon은 기존 호환 요청입니다. "
        "두 방식을 섞지 마세요. 커서 미연결로 결과가 limit보다 많으면 503입니다."
    ),
    responses={
        200: {"model": NearbyParkingResponse},
        **{code: {"model": ErrorResponse} for code in (422, 429, 500, 502, 503, 504)},
    },
)
async def nearby_request(
    request: Request,
    session: Session,
    service: Annotated[ParkingNearbyService, Depends(get_parking_nearby_service)],
    latitude: Annotated[float | None, Query(ge=-90, le=90, allow_inf_nan=False)] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180, allow_inf_nan=False)] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    lat: Annotated[float | None, Query(ge=37.4, le=37.75, deprecated=True)] = None,
    lon: Annotated[float | None, Query(ge=126.7, le=127.25, deprecated=True)] = None,
    source: Literal[
        "seoul_getparkinfo", "seoul_citydata_prk", "seoul_municipal_parking", "demo_parking"
    ]
    | None = None,
):
    if is_contract_query(request):
        if latitude is None or longitude is None:
            raise ContractError(422, "VALIDATION_ERROR", "latitude와 longitude가 모두 필요합니다.")
        if any(key in request.query_params for key in ("lat", "lon", "source")):
            raise ContractError(
                422, "VALIDATION_ERROR", "새 좌표와 기존 요청 필드를 함께 사용할 수 없습니다."
            )
        result = await service.search(
            Coordinates(latitude=latitude, longitude=longitude), limit, cursor
        )
        return envelope(result)
    missing = [key for key, value in (("lat", lat), ("lon", lon)) if value is None]
    if missing:
        raise HTTPException(
            422,
            detail=[
                {"loc": ["query", key], "msg": "Field required", "type": "missing"}
                for key in missing
            ],
        )
    return await nearby(session, lat, lon, source or SOURCE, limit)


router.include_router(nearby_router)


@router.get("/runs")
async def runs(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    records = await session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    )
    return [
        {column.name: getattr(run, column.name) for column in CollectionRun.__table__.columns}
        for run in records
    ]


@router.get("/municipal/raw")
async def municipal_raw(session: Session):
    from fastapi import HTTPException

    from app.models.provider import ProviderSnapshot

    snapshot = await session.get(ProviderSnapshot, "seoul_municipal_parking")
    if snapshot is None:
        raise HTTPException(404, "시영주차장 수집 기록이 없습니다.")
    return {"source": snapshot.source, "collected_at": snapshot.collected_at, "data": snapshot.data}


# Include after the static paths so /nearby and /runs are not parsed as UUIDs.
detail_router = APIRouter(route_class=ContractRoute)


@detail_router.get(
    "/{parking_id}",
    response_model=ParkingDetailResponse,
    summary="주차장 등록 상세 조회",
    responses={
        404: {"model": ParkingErrorResponse, "description": "주차장 없음 또는 제공 종료"},
        422: {"model": ParkingErrorResponse, "description": "잘못된 UUID"},
        503: {"model": ParkingErrorResponse, "description": "등록정보 조회 불가"},
    },
)
async def parking_detail(parking_id: UUID, session: Session):
    """Read active source registrations. Current vacancies are unsupported."""
    service = ParkingDetailService(ParkingDetailRepository(session))
    return envelope(await service.get_detail(parking_id))


router.include_router(detail_router)
