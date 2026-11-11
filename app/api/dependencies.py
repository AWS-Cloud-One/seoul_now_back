import hashlib
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.area_extras import SeoulAreaExtras
from app.clients.kakao import KakaoLocalClient
from app.clients.query_kakao import KakaoQueryClient
from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.modules.ai.providers.base import AIProvider
from app.modules.recommendation.engine import DestinationRecommendationEngine
from app.repositories.area_queries import AreaQueryRepository
from app.repositories.health import HealthRepository
from app.repositories.map import MapRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.services.area_queries import AreaQueryService
from app.services.chat import RecommendationChatProcessor
from app.services.current_place_answers import CurrentPlaceAnswers
from app.services.health import HealthService
from app.services.locations import LocationResolveService, LocationSearchService
from app.services.map import MapService
from app.services.origin_context import OriginContextStore
from app.services.origin_search import OriginSearchService
from app.services.parking_detail import ParkingDetailService
from app.services.parking_nearby import ParkingNearbyService
from app.services.parking_search import ParkingSearchService
from app.services.pending import PendingService
from app.services.place_details import PlaceDetailReader, TravelTimeReader
from app.services.place_queries import PlaceQueryService
from app.services.recommendation_adapters import (
    RecommendationConditionsValidator,
    RecommendationSource,
)
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendation_geography import RecommendationGeographyService
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


def get_parking_search_service(session: Session) -> ParkingSearchService:
    return ParkingSearchService(ParkingNearbyRepository(session))


def get_place_query_service(
    request: Request,
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PlaceQueryService:
    key = settings.kakao_rest_api_key.get_secret_value().strip()
    return PlaceQueryService(
        KakaoQueryClient(provider),
        driving_requests=request.app.state.driving_requests,
        cache_namespace=hashlib.sha256(key.encode()).hexdigest(),
    )


def get_place_detail_reader(
    contexts: Annotated[RecommendationContextService, Depends(get_context_reader)],
    parking: Annotated[ParkingNearbyService, Depends(get_parking_nearby_service)],
) -> PlaceDetailReader:
    return PlaceDetailReader(contexts, parking)


def get_travel_time_reader(
    request: Request,
    session: Session,
    contexts: Annotated[RecommendationContextService, Depends(get_context_reader)],
    queries: Annotated[PlaceQueryService, Depends(get_place_query_service)],
) -> TravelTimeReader:
    return TravelTimeReader(
        contexts,
        RecommendationConditionsRepository(session),
        queries,
        request.app.state.travel_replay,
    )


def get_area_query_service(
    session: Session,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AreaQueryService:
    return AreaQueryService(
        AreaQueryRepository(session),
        SeoulAreaExtras(http, settings.seoul_citydata_api_key.get_secret_value()),
    )


def get_ai_provider(request: Request) -> AIProvider:
    return request.app.state.ai_provider


def get_current_place_answers(
    places: Annotated[PlaceDetailReader, Depends(get_place_detail_reader)],
    travel: Annotated[TravelTimeReader, Depends(get_travel_time_reader)],
    areas: Annotated[AreaQueryService, Depends(get_area_query_service)],
) -> CurrentPlaceAnswers:
    return CurrentPlaceAnswers(places, travel, areas)


def get_recommendation_source(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
    parking: Annotated[ParkingNearbyService, Depends(get_parking_nearby_service)],
    queries: Annotated[PlaceQueryService, Depends(get_place_query_service)],
) -> RecommendationSource:
    return RecommendationSource(provider, parking, queries)


def get_recommendation_execution(request: Request) -> RecommendationExecution:
    return RecommendationExecution(
        request.app.state.recommendation_progress,
        RecommendationPersistence(
            request.app.state.session_factory,
            request.app.state.write_session_factory,
            context_ttl_seconds=request.app.state.settings.result_context_ttl_seconds,
        ),
    )


def get_recommendation_geography(
    session: Session,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RecommendationGeographyService:
    return RecommendationGeographyService(
        RecommendationGeographyRepository(session),
        SeoulAreaExtras(http, settings.seoul_citydata_api_key.get_secret_value()),
    )


def get_recommendation_service(
    source: Annotated[RecommendationSource, Depends(get_recommendation_source)],
    model: Annotated[AIProvider, Depends(get_ai_provider)],
    execution: Annotated[RecommendationExecution, Depends(get_recommendation_execution)],
    geography: Annotated[RecommendationGeographyService, Depends(get_recommendation_geography)],
) -> RecommendationService:
    return RecommendationService(
        RecommendationConditionsValidator(source),
        DestinationRecommendationEngine(
            source,
            model,
            area_provider=geography,
            traffic_provider=geography,
        ),
        execution=execution,
    )


def get_origin_context_store(request: Request) -> OriginContextStore:
    return request.app.state.origin_contexts


def get_origin_search_service(
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
) -> OriginSearchService:
    return OriginSearchService(provider)


def get_pending_service(
    model: Annotated[AIProvider, Depends(get_ai_provider)],
    provider: Annotated[KakaoLocalClient, Depends(get_kakao_local_client)],
    recommendations: Annotated[RecommendationService, Depends(get_recommendation_service)],
    contexts: Annotated[RecommendationContextService, Depends(get_context_reader)],
    reasons: Annotated[RecommendationReasonService, Depends(get_recommendation_reason_service)],
    execution: Annotated[RecommendationExecution, Depends(get_recommendation_execution)],
    places: Annotated[PlaceDetailReader, Depends(get_place_detail_reader)],
    travel: Annotated[TravelTimeReader, Depends(get_travel_time_reader)],
    current_answers: Annotated[CurrentPlaceAnswers, Depends(get_current_place_answers)],
    origin_contexts: Annotated[OriginContextStore, Depends(get_origin_context_store)],
    origin_search: Annotated[OriginSearchService, Depends(get_origin_search_service)],
) -> PendingService:
    return PendingService(
        places=places,
        travel=travel,
        chat=RecommendationChatProcessor(
            model,
            provider,
            recommendations,
            execution=execution,
            contexts=contexts,
            reasons=reasons,
            current_answers=current_answers,
            origin_contexts=origin_contexts,
            origin_search=origin_search,
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
