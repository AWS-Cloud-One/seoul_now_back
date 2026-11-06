import asyncio
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session, get_settings
from app.clients.errors import ProviderError
from app.clients.kakao import KakaoClient
from app.core.config import Settings
from app.modules.ai.providers.openai import OpenAIProvider
from app.modules.recommendation.state import StateCodec
from app.repositories.recommendation import RecommendationEvidenceRepository
from app.schemas.recommendation import ChatRequest, ChatResponse
from app.services.recommendation import RecommendationService

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


async def get_recommendation_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    async with httpx.AsyncClient() as client:
        yield RecommendationService(
            OpenAIProvider(client, settings),
            KakaoClient(client, settings),
            RecommendationEvidenceRepository(session),
            StateCodec(request.app.state.recommendation_state_secret),
        )


@router.post("", response_model=ChatResponse)
async def recommend_places(
    body: ChatRequest,
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
):
    try:
        async with asyncio.timeout(90):
            return await service.recommend(body)
    except ProviderError as error:
        raise HTTPException(
            error.status_code, {"code": error.code, "message": error.message}
        ) from None
    except ValueError:
        raise HTTPException(
            422,
            {
                "code": "invalid_recommendation_request",
                "message": "검색 조건 또는 상태가 유효하지 않습니다. 새 검색을 시작해 주세요.",
            },
        ) from None
    except TimeoutError:
        raise HTTPException(
            504,
            {
                "code": "recommendation_timeout",
                "message": "추천 조회 시간이 초과되었습니다. 다시 시도해 주세요.",
            },
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            503, {"code": "evidence_unavailable", "message": "추천 근거 자료를 조회할 수 없습니다."}
        ) from None
