"""R01 structured recommendation request/response boundary."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.contract import ContractRoute, envelope
from app.api.dependencies import get_recommendation_service
from app.api.errors import api_error, error_status
from app.api.recommendation_dependencies import get_natural_recommendation_service
from app.modules.recommendation.errors import ProviderError
from app.schemas.recommendation import ChatRequest, ChatResponse
from app.schemas.recommendations import RecommendationEnvelope, RecommendationRequest
from app.services.recommendation import RecommendationService as NaturalRecommendationService
from app.services.recommendations import RecommendationService

router = APIRouter(prefix="/recommendations", tags=["recommendations"], route_class=ContractRoute)


@router.post(
    "",
    response_model=ChatResponse | RecommendationEnvelope,
    summary="자연어 또는 구조화 조건 기반 목적지 추천",
    description=(
        "message/conditions/state 요청은 GPT 자연어 추천입니다. "
        "기존 구조화 R01 요청은 별도 처리하며 조건 해석 AI를 호출하지 않습니다."
    ),
    responses={
        code: {"model": RecommendationEnvelope}
        for code in (400, 409, 413, 422, 429, 500, 502, 503, 504)
    },
)
async def recommendations(
    body: Annotated[
        ChatRequest | RecommendationRequest,
        Body(
            openapi_examples={
                "natural_first": {
                    "summary": "시청역 한식집 자연어 추천",
                    "value": {
                        "message": "가족끼리 먹을 한식집 추천해줘",
                        "conditions": {"x": 126.977, "y": 37.5657, "max_minutes": 30},
                    },
                },
                "natural_more": {
                    "summary": "다른 가게 추천",
                    "value": {
                        "message": "다른 가게 추천해줘",
                        "state": "직전 응답의 state 문자열",
                    },
                },
            }
        ),
    ],
    response: Response,
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
    natural: Annotated[NaturalRecommendationService, Depends(get_natural_recommendation_service)],
):
    if isinstance(body, ChatRequest):
        response.headers["Cache-Control"] = "no-store"
        try:
            async with asyncio.timeout(90):
                return await natural.recommend(body)
        except ProviderError as exc:
            status, code, message = exc.status_code, exc.code, exc.message
        except ValueError:
            status, code, message = (
                422,
                "invalid_recommendation_request",
                "검색 조건 또는 상태를 확인해 주세요.",
            )
        except TimeoutError:
            status, code, message = (
                504,
                "recommendation_timeout",
                "추천 조회 시간이 초과되었습니다.",
            )
        except SQLAlchemyError:
            status, code, message = (
                503,
                "evidence_unavailable",
                "추천 근거 자료를 조회할 수 없습니다.",
            )
        return JSONResponse(
            status_code=status,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": code, "message": message}},
        )
    outcome = await service.execute(body)
    error = api_error(outcome.failure) if outcome.failure else None
    response.status_code = error_status(outcome.failure) if outcome.failure else 200
    response.headers["Cache-Control"] = "no-store"
    if error is not None and error.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(error.retry_after_seconds)
    return envelope(outcome.response, error, request_id=body.request_id)
