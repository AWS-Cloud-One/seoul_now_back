"""R01 structured recommendation request/response boundary."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_recommendation_service
from app.api.errors import api_error, error_status
from app.schemas.recommendations import RecommendationEnvelope, RecommendationRequest
from app.services.recommendations import RecommendationService

router = APIRouter(prefix="/recommendations", tags=["recommendations"], route_class=ContractRoute)


@router.post(
    "",
    response_model=RecommendationEnvelope,
    summary="구조화 조건 기반 목적지 추천",
    description=(
        "구조화 조건을 검증하고 후보·경로·등록 주차 정보를 비교합니다. "
        "조건 해석 AI는 호출하지 않습니다."
    ),
    responses={
        code: {"model": RecommendationEnvelope}
        for code in (400, 409, 413, 422, 429, 500, 502, 503, 504)
    },
)
async def recommendations(
    body: RecommendationRequest,
    response: Response,
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
):
    outcome = await service.execute(body)
    error = api_error(outcome.failure) if outcome.failure else None
    response.status_code = error_status(outcome.failure) if outcome.failure else 200
    response.headers["Cache-Control"] = "no-store"
    if error is not None and error.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(error.retry_after_seconds)
    return envelope(outcome.response, error, request_id=body.request_id)
