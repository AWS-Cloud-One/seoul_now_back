from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.kakao import KakaoLocalClient
from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.modules.ai.providers.openai import OpenAIProvider
from app.modules.recommendation.engine import DestinationRecommendationEngine
from app.repositories.health import HealthRepository
from app.repositories.map import MapRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.chat import RecommendationChatProcessor
from app.services.health import HealthService
from app.services.locations import LocationResolveService, LocationSearchService
from app.services.map import MapService
from app.services.parking_detail import ParkingDetailService
from app.services.parking_nearby import ParkingNearbyService
from app.services.pending import PendingService
from app.services.recommendation_adapters import (
    RecommendationConditionsValidator,
    RecommendationSource,
)
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendation_persistence import RecommendationPersistence
from app.services.recommendation_reason import RecommendationReasonService
from app.services.recommendation_retry import RecommendationRouteRetryProcessor
from app.services.recommendations import RecommendationService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # The API factory uses REPEATABLE READ / READ ONLY. Closing releases the request snapshot.
    async with request.app.state.session_factory() as session:
        yield session


async def get_write_session(request: Request) -> AsyncIterator[AsyncSession]:
    # The service must use session.begin(); never commit after yielding an HTTP response.
    async with request.app.state.write_session_factory() as session:
        yield session


def get_health_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthService:
    repository = HealthRepository(session)
    return HealthService(repository, timeout_seconds=settings.health_timeout_seconds)


Session = Annotated[AsyncSession, Depends(get_session)]
WriteSession = Annotated[AsyncSession, Depends(get_write_session)]


def get_context_reader(
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RecommendationContextService:
    return RecommendationContextService(
        RecommendationContextRepository(session),
        ttl_seconds=settings.result_context_ttl_seconds,
    )


def get_context_writer(
    session: WriteSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RecommendationContextService:
    # The invoking orchestrator owns session.begin() and commits before returning its response.
    return RecommendationContextService(
        RecommendationContextRepository(session),
        ttl_seconds=settings.result_context_ttl_seconds,
    )


def get_recommendation_reason_service(
    contexts: Annotated[RecommendationContextService, Depends(get_context_reader)],
) -> RecommendationReasonService:
    return RecommendationReasonService(contexts)


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=10) as http:
        yield http


def get_kakao_local_client(
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KakaoLocalClient:
    return KakaoLocalClient(http, settings.kakao_rest_api_key.get_secret_value())


def get_parking_detail_service(session: Session) -> ParkingDetailService:
    return ParkingDetailService(ParkingDetailRepository(session))


def get_map_service(session: Session) -> MapService:
    return MapService(MapRepository(session))


def get_location_search_service(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> LocationSearchService:
    return LocationSearchService(provider)


def get_location_resolve_service(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> LocationResolveService:
    return LocationResolveService(provider)


def get_parking_nearby_service(
    session: Session,
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> ParkingNearbyService:
    return ParkingNearbyService(ParkingNearbyRepository(session), provider)


def get_ai_provider(
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenAIProvider:
    return OpenAIProvider(http, settings)


def get_recommendation_source(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
    parking: Annotated[ParkingNearbyService, Depends(get_parking_nearby_service)],
) -> RecommendationSource:
    return RecommendationSource(provider, parking)


def get_recommendation_execution(request: Request) -> RecommendationExecution:
    return RecommendationExecution(
        request.app.state.recommendation_progress,
        RecommendationPersistence(
            request.app.state.session_factory,
            request.app.state.write_session_factory,
            context_ttl_seconds=request.app.state.settings.result_context_ttl_seconds,
        ),
    )


def get_recommendation_service(
    source: Annotated[RecommendationSource, Depends(get_recommendation_source)],
    model: Annotated[OpenAIProvider, Depends(get_ai_provider)],
    execution: Annotated[RecommendationExecution, Depends(get_recommendation_execution)],
) -> RecommendationService:
    return RecommendationService(
        RecommendationConditionsValidator(source),
        DestinationRecommendationEngine(source, model),
        execution=execution,
    )


def get_pending_service(
    model: Annotated[OpenAIProvider, Depends(get_ai_provider)],
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
    recommendations: Annotated[RecommendationService, Depends(get_recommendation_service)],
    contexts: Annotated[RecommendationContextService, Depends(get_context_reader)],
    reasons: Annotated[RecommendationReasonService, Depends(get_recommendation_reason_service)],
    execution: Annotated[RecommendationExecution, Depends(get_recommendation_execution)],
) -> PendingService:
    # Current fact readers and administrative status await their owning parts.
    return PendingService(
        chat=RecommendationChatProcessor(
            model,
            provider,
            recommendations,
            execution=execution,
            contexts=contexts,
            reasons=reasons,
        ),
        retry=RecommendationRouteRetryProcessor(
            recommendations, contexts=contexts, execution=execution
        ),
    )


async def require_data_status_access():
    """Replace with agreed admin authorization; default denies all access."""
    raise ServiceError(
        "SERVICE_UNAVAILABLE",
        "관리자 접근 보호 방식이 아직 연결되지 않았습니다.",
        retryable=False,
    )
