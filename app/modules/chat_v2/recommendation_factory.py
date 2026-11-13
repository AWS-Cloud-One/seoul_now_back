"""Request-scoped V2 recommendation reads; no persistence or HTTP route registration."""

import hashlib
from contextlib import asynccontextmanager

import httpx

from app.clients.area_extras import SeoulAreaExtras
from app.clients.kakao import KakaoLocalClient
from app.clients.query_kakao import KakaoQueryClient
from app.core.config import Settings
from app.modules.chat_v2.contracts import RunIdentity
from app.modules.chat_v2.preparation import ExistingRecommendationPreparer
from app.modules.chat_v2.regional_preparation import DestinationRecommendationPreparer
from app.modules.recommendation.engine import DestinationRecommendationEngine
from app.modules.recommendation.progress import RecommendationWork
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.services.driving_requests import DrivingRequests
from app.services.parking_nearby import ParkingNearbyService
from app.services.place_queries import PlaceQueryService
from app.services.query_cursors import QueryCursorStore
from app.services.recommendation_adapters import (
    RecommendationConditionsValidator,
    RecommendationSource,
)
from app.services.recommendation_geography import RecommendationGeographyService
from app.services.recommendations import RecommendationService


@asynccontextmanager
async def open_recommendation_preparer(
    readers,
    http: httpx.AsyncClient,
    settings: Settings,
    *,
    identity: RunIdentity,
    work: RecommendationWork,
    driving_requests: DrivingRequests,
):
    """Supply read_session_factory and a process-shared route budget/cache.

    Query cursors are private to this preparation and are not published as V2
    actions. The execution owner must provide an already authorized workspace.
    Model-based explanation is a later step; existing factual fallback text is used.
    """
    async with readers() as session:
        key = settings.kakao_rest_api_key.get_secret_value()
        kakao = KakaoLocalClient(http, key)
        cursors = QueryCursorStore()
        parking = ParkingNearbyService(ParkingNearbyRepository(session), kakao, cursors=cursors)
        queries = PlaceQueryService(
            KakaoQueryClient(kakao),
            cursors=cursors,
            driving_requests=driving_requests,
            cache_namespace=hashlib.sha256(key.strip().encode()).hexdigest(),
        )
        source = RecommendationSource(kakao, parking, queries)
        geography = RecommendationGeographyService(
            RecommendationGeographyRepository(session),
            SeoulAreaExtras(http, settings.seoul_citydata_api_key.get_secret_value()),
        )
        service = RecommendationService(
            RecommendationConditionsValidator(source),
            DestinationRecommendationEngine(
                source, area_provider=geography, traffic_provider=geography
            ),
        )
        yield DestinationRecommendationPreparer(
            ExistingRecommendationPreparer(service, identity=identity, work=work),
            session,
            source,
            geography,
            identity=identity,
            work=work,
        )
