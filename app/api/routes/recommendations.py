"""R01 structured recommendation request/response boundary."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.contract import ContractRoute
from app.schemas.recommendations import RecommendationEnvelope, RecommendationRequest
from app.services.recommendations import RecommendationService, get_recommendation_service

router = APIRouter(prefix="/recommendations", tags=["recommendations"], route_class=ContractRoute)


@router.post(
    "",
    response_model=RecommendationEnvelope,
    summary="구조화 조건 기반 목적지 추천",
    description=(
        "부분 초안. 검증·추천 어댑터 연결 전에는 503을 반환합니다. "
        "조건 해석 AI는 호출하지 않습니다."
    ),
    responses={
        code: {"model": RecommendationEnvelope}
        for code in (400, 409, 413, 422, 429, 500, 502, 503, 504)
    },
)
async def recommendations(
    body: RecommendationRequest,
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
):
    status, result = await service.execute(body)
    headers = {"Cache-Control": "no-store"}
    if result.error is not None and result.error.retry_after_seconds is not None:
        headers["Retry-After"] = str(result.error.retry_after_seconds)
    return JSONResponse(status_code=status, content=result.model_dump(mode="json"), headers=headers)
