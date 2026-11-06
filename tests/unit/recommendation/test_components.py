import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.clients.kakao import KakaoLocalClient
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.cards import unavailable_parking
from app.modules.recommendation.ranking import rank_candidates
from app.modules.recommendation.types import Candidate
from app.schemas.location import Coordinates
from app.schemas.pending import Place
from app.services.recommendation_adapters import RecommendationSource


def candidate(identifier, seconds, meters):
    parking = unavailable_parking()
    if meters is not None:
        parking.status = "available"
        parking.nearest_distance_meters = meters
    return Candidate(
        place=Place(
            place_id=identifier,
            name=identifier,
            purpose="cafe",
            category_code="CE7",
            category_name=None,
            coordinates=Coordinates(latitude=37.56, longitude=126.97),
            address=None,
            road_address=None,
            phone=None,
            provider_url=None,
        ),
        collected_at=datetime.now(UTC),
        duration_seconds=seconds,
        distance_meters=1000,
        parking=parking,
    )


@pytest.mark.parametrize(
    "inputs,expected",
    [
        ([("b", 600, 10), ("a", 659, 30)], ["b", "a"]),
        ([("b", 600, 10), ("a", 659, None)], ["a", "b"]),
        ([("a", 660, 1), ("b", 659, 100)], ["b", "a"]),
        ([("2", 600, 20), ("10", 601, 20)], ["10", "2"]),
    ],
)
def test_minute_bucket_parking_and_lexical_order(inputs, expected):
    assert [
        c.place.place_id for c in rank_candidates([candidate(*values) for values in inputs])
    ] == expected


def test_interpretation_distinguishes_omission_from_explicit_clear():
    assert Interpretation(intent="recommendation").patch().model_dump(exclude_unset=True) == {}
    assert Interpretation(
        intent="recommendation", clear_fields=["max_travel_seconds"]
    ).patch().model_dump(exclude_unset=True) == {"max_travel_seconds": None}
    with pytest.raises(ServiceError):
        Interpretation(intent="recommendation", headcount=2, clear_fields=["headcount"]).patch()


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (503, "PROVIDER_ERROR")])
def test_shared_kakao_transport_errors_reach_recommendation_ports(status, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, headers={"Retry-After": "8"})
            )
        ) as http:
            source = RecommendationSource(KakaoLocalClient(http, "test-key"), AsyncMock())
            with pytest.raises(ServiceError) as exc:
                await source.route(
                    Coordinates(latitude=37.56, longitude=126.97),
                    Coordinates(latitude=37.57, longitude=126.98),
                )
            assert exc.value.code == code
            if status == 429:
                assert exc.value.retry_after_seconds == 8

    asyncio.run(run())


@pytest.mark.parametrize(
    "summary",
    [
        {"duration": True, "distance": 1},
        {"duration": 10, "distance": -1},
        {"duration": -1, "distance": 10},
        {"duration": 10},
    ],
)
def test_invalid_route_numbers_are_not_ranked(summary):
    async def run():
        kakao = AsyncMock()
        kakao.directions.return_value = {"routes": [{"result_code": 0, "summary": summary}]}
        source = RecommendationSource(kakao, AsyncMock())
        with pytest.raises(ServiceError, match="이동시간"):
            await source.route(
                Coordinates(latitude=37.56, longitude=126.97),
                Coordinates(latitude=37.57, longitude=126.98),
            )

    asyncio.run(run())


def test_recommendation_kakao_calls_preserve_coordinates_and_provider_identity():
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("keyword.json"):
                return httpx.Response(
                    200,
                    json={
                        "documents": [
                            {
                                "id": "001",
                                "place_name": "카페",
                                "x": "126.98",
                                "y": "37.57",
                                "category_group_code": "CE7",
                            }
                        ],
                        "meta": {"is_end": True},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "result_code": 0,
                            "summary": {
                                "duration": 600,
                                "distance": 1500,
                            },
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            source = RecommendationSource(KakaoLocalClient(http, "test-key"), AsyncMock())
            origin = Coordinates(latitude=37.56, longitude=126.97)
            values, end = await source.search("카페", origin, "cafe", 2)
            assert end and values[0].place.place_id == "kakao:001"
            assert requests[0].url.params["page"] == "2"
            assert requests[0].url.params["x"] == "126.97"
            assert await source.route(origin, values[0].place.coordinates) == (600, 1500)
            assert requests[1].url.params["origin"] == "126.97,37.56"
            assert requests[1].url.params["destination"] == "126.98,37.57"

    asyncio.run(run())


def test_parking_database_failure_becomes_a_shared_service_error():
    async def run():
        parking = AsyncMock()
        parking.search.side_effect = SQLAlchemyError("private database details")
        source = RecommendationSource(AsyncMock(), parking)
        with pytest.raises(ServiceError) as exc:
            await source.parking(Coordinates(latitude=37.56, longitude=126.97))
        assert exc.value.code == "DATA_UNAVAILABLE"
        assert "private database details" not in exc.value.message

    asyncio.run(run())
