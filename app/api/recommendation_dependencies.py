"""Natural recommendation wiring, separate from shared API dependencies."""

import secrets
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session, get_settings
from app.clients.recommendation_kakao import KakaoClient
from app.core.config import Settings
from app.modules.ai.providers.openai import OpenAIProvider
from app.modules.recommendation.settings import RecommendationSettings
from app.modules.recommendation.state import StateCodec
from app.repositories.recommendation import RecommendationEvidenceRepository
from app.services.recommendation import RecommendationService


async def get_natural_recommendation_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    config = RecommendationSettings()
    if not hasattr(request.app.state, "recommendation_state_secret"):
        request.app.state.recommendation_state_secret = (
            config.recommendation_state_secret.get_secret_value() or secrets.token_urlsafe(32)
        )
    async with httpx.AsyncClient(timeout=10) as client:
        yield RecommendationService(
            OpenAIProvider(client, config),
            KakaoClient(client, settings),
            RecommendationEvidenceRepository(session),
            StateCodec(request.app.state.recommendation_state_secret),
        )
