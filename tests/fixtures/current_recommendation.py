"""Run the real recommendation engine with deterministic provider facts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from app.core.exceptions import ServiceError
from app.modules.recommendation.areas import SearchArea
from app.modules.recommendation.engine import DestinationRecommendationEngine
from app.modules.recommendation.progress import MemoryProgressStore
from app.modules.recommendation.types import Candidate
from app.schemas.parking_nearby import ParkingSummary
from app.schemas.recommendations import RecommendationRequest
from app.services.recommendation_adapters import RecommendationConditionsValidator
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendations import RecommendationService
from tests.fixtures.recommendation_context import response


async def current_result(*, traffic="fresh", hidden_unknown=False, general=False, areas=True):
    now = datetime.now(UTC)
    base = response()
    request = RecommendationRequest(
        request_id=uuid4(),
        trip_id=base.trip_id,
        condition_version=0,
        current_conditions=base.condition_update.conditions,
        conditions_patch={},
        excluded_place_ids=[],
    )
    values, facts = [], {}
    rows = [("a", 1210, "정체", 800), ("b", 1320, "서행", 800), ("c", 1490, "정체", 100)]
    if hidden_unknown:
        rows.append(("d", 1499, "확인 불가", 100))
    for i, (key, seconds, grade, distance) in enumerate(rows):
        place = dict(base.result["cards"][0]["place"]["value"])
        place.update(place_id=f"kakao:{key}", name=key)
        place["coordinates"] = {"latitude": 37.57, "longitude": 126.98 + i * 0.001}
        observed = now - timedelta(minutes=31 if traffic == "stale" else 5)
        if traffic == "future":
            observed = now + timedelta(minutes=1)
        values.append(
            Candidate(
                place=place,
                collected_at=now,
                congestion=grade,
                congestion_observed_at=observed,
                congestion_source=None if traffic == "missing" else "verified-road",
            )
        )
        facts[place["coordinates"]["longitude"]] = seconds, distance
    source = AsyncMock()
    source.region.return_value = "seoul"
    source.search.return_value = (values, True)
    source.route.side_effect = (
        ServiceError("PROVIDER_TIMEOUT", "경로 확인 실패")
        if general
        else lambda origin, destination: (facts[destination.longitude][0], 1000)
    )

    def parking(coordinates):
        distance = facts[coordinates.longitude][1]
        return ParkingSummary(
            status="available",
            reason_code=None,
            checked_at=now,
            search_radius_meters=300 if distance <= 300 else 1000,
            nearest_distance_meters=distance,
            items=[],
            total_count=1,
            next_cursor=None,
        )

    source.parking.side_effect = parking
    provider = AsyncMock() if areas else None
    if provider:
        provider.areas.return_value = [
            SearchArea(
                area_id="1101053",
                name="선정 동",
                center={"latitude": 37.57, "longitude": 126.98},
                rings=[[(126.97, 37.56), (126.99, 37.56), (126.99, 37.58), (126.97, 37.58)]],
                source="provided-boundaries",
                geometry_version="2026-v1",
                congestion="서행",
                road_observed_at=now - timedelta(minutes=2),
                registered_parking_count=7,
                parking_checked_at=now,
            )
        ]
    engine = DestinationRecommendationEngine(
        source,
        area_provider=provider,
    )
    service = RecommendationService(
        RecommendationConditionsValidator(source),
        engine,
        execution=RecommendationExecution(MemoryProgressStore()),
    )
    outcome = await service.execute(request)
    assert outcome.failure is None
    return SimpleNamespace(request=request, data=outcome.response, source=source, service=service)
