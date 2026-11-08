"""Provisional HTTP boundaries; injected team services supply actual behavior."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_pending_service, require_data_status_access
from app.api.errors import ERROR_STATUS, api_error, error_status
from app.schemas.common import ErrorResponse
from app.schemas.pending import (
    ChatRequest,
    ChatResponse,
    DataStatusResponse,
    PlaceDetailResponse,
    RetryRoutesRequest,
    TravelTimeRequest,
    TravelTimeResponse,
)
from app.schemas.recommendations import RecommendationEnvelope
from app.services.pending import ConversationOutcome, PendingService

router = APIRouter(route_class=ContractRoute)
Service = Annotated[PendingService, Depends(get_pending_service)]
PlaceId = Annotated[str, Path(min_length=1)]
# Adapters use the shared ServiceError mapping for every provisional endpoint.
ERRORS = {status: {"model": ErrorResponse} for status in sorted(set(ERROR_STATUS.values()))}


def conversation_response(outcome: ConversationOutcome, response: Response, request_id: UUID):
    error = api_error(outcome.failure) if outcome.failure else None
    response.status_code = error_status(outcome.failure) if outcome.failure else 200
    response.headers["Cache-Control"] = "no-store"
    if error is not None and error.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(error.retry_after_seconds)
    return envelope(outcome.response, error, request_id=request_id)


@router.post(
    "/chat/messages",
    tags=["chat"],
    response_model=ChatResponse,
    summary="채팅·조건 변경·추천",
    description="질문과 조건 변경을 해석해 추천하거나 선택한 카드의 당시 추천 이유를 답합니다.",
    responses={code: {"model": ChatResponse} for code in ERRORS},
)
async def chat_messages(body: ChatRequest, response: Response, service: Service):
    return conversation_response(await service.conversation(body), response, body.request_id)


@router.post(
    "/recommendations/retry-routes",
    tags=["recommendations"],
    response_model=RecommendationEnvelope,
    summary="같은 후보 경로 재확인 — 연결 틀",
    description="보류 계약. 문맥 검증·경로 서비스 미연결 시 503. 새 후보를 탐색하지 않습니다.",
    responses={code: {"model": RecommendationEnvelope} for code in ERRORS},
)
async def retry_routes(body: RetryRoutesRequest, response: Response, service: Service):
    return conversation_response(
        await service.conversation(body, retry=True), response, body.request_id
    )


@router.get(
    "/places/{place_id}",
    tags=["places"],
    response_model=PlaceDetailResponse,
    summary="추천 장소 상세 — 연결 틀",
    responses=ERRORS,
    description="보류 계약. Result-Context 필수. 문맥 검증·상세 서비스 미연결 시 503입니다.",
)
async def place_detail(
    place_id: PlaceId,
    service: Service,
    result_context: Annotated[str, Header(alias="Result-Context", min_length=1)],
):
    return envelope(await service.place_detail(place_id, result_context))


@router.post(
    "/places/{place_id}/travel-time",
    tags=["places"],
    response_model=TravelTimeResponse,
    summary="이동시간 재확인 — 연결 틀",
    responses=ERRORS,
    description="보류 계약. 출발지·목적지는 검증된 문맥에서 얻습니다. 미연결 시 503입니다.",
)
async def travel_time(
    place_id: PlaceId, body: TravelTimeRequest, response: Response, service: Service
):
    response.headers["Cache-Control"] = "no-store"
    return envelope(await service.travel_time(place_id, body), request_id=body.request_id)


@router.get(
    "/data-status",
    tags=["admin"],
    response_model=DataStatusResponse,
    summary="관리자 수집 상태 — 비공개 연결 틀",
    responses=ERRORS,
    dependencies=[Depends(require_data_status_access)],
    description="보류·비공개 계약. 관리자 인증 미연결 상태에서는 항상 차단(503)합니다.",
)
async def data_status(
    service: Service,
    source: Annotated[str, Query(min_length=1)],
    target_key: Annotated[str, Query(min_length=1)],
):
    return envelope(await service.data_status(source, target_key))
