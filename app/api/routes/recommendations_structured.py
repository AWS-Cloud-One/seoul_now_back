"""R01 structured recommendation entry point; natural-language interpretation is separate."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session, get_settings
from app.clients.kakao import KakaoClient
from app.core.config import Settings
from app.modules.ai.providers.openai import OpenAIProvider
from app.repositories.recommendation import RecommendationEvidenceRepository
from app.schemas.recommendations import RecommendationEnvelope, RecommendationRequest
from app.services.recommendations import StructuredRecommendationService


class RecommendationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                request_id = uuid4()
                if isinstance(exc.body, dict):
                    try:
                        request_id = UUID(str(exc.body.get("request_id")))
                    except ValueError:
                        pass
                malformed = any(e["type"] == "json_invalid" for e in exc.errors())
                return JSONResponse(
                    status_code=400 if malformed else 422,
                    headers={"Cache-Control": "no-store"},
                    content=dict(
                        request_id=str(request_id),
                        server_time=datetime.now(UTC).isoformat(),
                        data=None,
                        error=dict(
                            code="MALFORMED_REQUEST" if malformed else "VALIDATION_ERROR",
                            message="요청 형식과 조건을 확인해 주세요.",
                            retryable=False,
                            retry_after_seconds=None,
                            details=[
                                dict(
                                    field=".".join(map(str, e["loc"])),
                                    code=e["type"],
                                    message="입력값이 유효하지 않습니다.",
                                )
                                for e in exc.errors()
                            ],
                        ),
                    ),
                )

        return handler


router = APIRouter(
    prefix="/recommendations", tags=["recommendations"], route_class=RecommendationRoute
)


async def get_recommendation_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    async with httpx.AsyncClient() as client:
        yield StructuredRecommendationService(
            OpenAIProvider(client, settings),
            KakaoClient(client, settings),
            RecommendationEvidenceRepository(session),
            request.app.state.recommendation_sessions,
        )


@router.post(
    "",
    response_model=RecommendationEnvelope,
    summary="구조화된 조건 기반 장소 추천",
    description=(
        "조건 해석 AI를 호출하지 않습니다. 추가 추천은 conditions_patch={}이며, "
        "max_travel_seconds=null은 제한 없음입니다."
    ),
)
async def recommend_places(
    body: RecommendationRequest,
    service: Annotated[StructuredRecommendationService, Depends(get_recommendation_service)],
):
    status, payload = await service.execute(body)
    return JSONResponse(status_code=status, content=payload, headers={"Cache-Control": "no-store"})
