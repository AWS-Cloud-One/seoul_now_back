from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.routes.recommendations_structured import get_recommendation_service, router
from app.clients.errors import ProviderError
from app.modules.ai.explanation import ExplanationPlan
from app.modules.recommendation.sessions import RecommendationSessions
from app.schemas.recommendation import ParkingEvidence, Place, TrafficEvidence
from app.services.recommendations import StructuredRecommendationService


class Model:
    def __init__(self):
        self.calls = 0

    async def complete(self, instructions, payload, output_type):
        assert output_type is ExplanationPlan  # R01 must never interpret natural language.
        self.calls += 1
        return ExplanationPlan(
            items=[
                dict(place_id=c["place_id"], facts=list(c["facts"]))
                for c in payload["ordered_candidates"]
            ]
        )


class Kakao:
    def __init__(self):
        self.searches, self.routes = [], []

    async def region(self, x, y):
        return "seoul"

    async def search(self, conditions, page):
        self.searches.append((conditions.query, page))
        ids = ["1", "2", "3", "4", "5", "6"] if page == 1 else ["1", "7", "8"]
        return [Place(id=i, name=conditions.query + i, x=127, y=37.5) for i in ids], page == 2

    async def route(self, conditions, place):
        self.routes.append(place.id)
        return 600 + int(place.id) * 10, 1000


class Evidence:
    async def load(self):
        pass

    async def for_place(self, place):
        return TrafficEvidence(), None


@pytest.fixture
def app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def dependencies(app):
    service = StructuredRecommendationService(
        Model(), Kakao(), Evidence(), RecommendationSessions()
    )
    app.dependency_overrides[get_recommendation_service] = lambda: service
    return service


def body():
    return dict(
        request_id=str(uuid4()),
        trip_id=str(uuid4()),
        condition_version=0,
        current_conditions=dict(
            origin=None,
            purposes=[],
            group_type=None,
            age_groups=None,
            headcount=None,
            max_travel_seconds=None,
        ),
        excluded_place_ids=[],
        conditions_patch=dict(
            origin=dict(
                label="시청역",
                coordinates=dict(latitude=37.5657, longitude=126.977),
                address=None,
                region="seoul",
                supported=True,
            ),
            purposes=[dict(purpose="food", keyword="한식집")],
            max_travel_seconds=1800,
        ),
    )


def send(client, request):
    return client.post("/api/v1/recommendations", json=request)


def cards(response):
    return response["data"]["result"]["cards"]


def ids(response):
    return [c["place"]["value"]["place_id"] for c in cards(response)]


def next_request(previous, response, patch=None):
    update = response["data"]["condition_update"]
    return dict(
        request_id=str(uuid4()),
        trip_id=previous["trip_id"],
        condition_version=update["version"],
        current_conditions=update["conditions"],
        excluded_place_ids=list(
            dict.fromkeys(
                previous["excluded_place_ids"]
                + (ids(response) if response["data"]["kind"] == "recommendation" else [])
            )
        ),
        conditions_patch=patch or {},
    )


def test_first_more_change_and_no_interpretation(dependencies, client):
    req = body()
    response = send(client, req)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    one = response.json()
    assert ids(one) == ["kakao:1", "kakao:2", "kakao:3"]
    assert one["data"]["stages"]["interpretation"] == "skipped"
    assert one["data"]["condition_update"]["version"] == 1
    req2 = next_request(req, one)
    two = send(client, req2).json()
    assert ids(two) == ["kakao:4", "kakao:5", "kakao:6"]
    assert two["data"]["condition_update"]["version"] == 1
    assert two["data"]["condition_update"]["changes"] == []
    assert dependencies.kakao.searches == [("한식집", 1)]
    three = send(
        client, next_request(req2, two, {"purposes": [{"purpose": "food", "keyword": "파스타"}]})
    ).json()
    assert ids(three) == ["kakao:7", "kakao:8"]
    assert three["data"]["condition_update"]["version"] == 2
    assert dependencies.kakao.searches[-2:] == [("파스타", 1), ("파스타", 2)]


def test_null_limit_and_seconds_boundary(dependencies, client):
    dependencies.kakao.route = AsyncMock(
        side_effect=lambda c, p: (1800 if p.id == "1" else 1801, 500)
    )
    req = body()
    result = send(client, req).json()
    assert ids(result) == ["kakao:1"]
    req2 = next_request(req, result, {"max_travel_seconds": None})
    unlimited = send(client, req2).json()
    assert unlimited["data"]["condition_update"]["conditions"]["max_travel_seconds"] is None
    assert all(c["route"]["value"]["duration_seconds"] == 1801 for c in cards(unlimited))


def test_omitted_limit_inherits_null_not_thirty(dependencies, client):
    req = body()
    del req["conditions_patch"]["max_travel_seconds"]
    dependencies.kakao.route = AsyncMock(return_value=(7200, 10000))
    result = send(client, req).json()
    assert len(cards(result)) == 3
    assert result["data"]["condition_update"]["conditions"]["max_travel_seconds"] is None


def test_all_routes_failed_are_unranked_general_candidates(dependencies, client):
    dependencies.kakao.route = AsyncMock(side_effect=ProviderError(504, "kakao_timeout", "timeout"))
    req = body()
    result = send(client, req).json()
    assert result["data"]["kind"] == "general_candidates"
    assert result["data"]["stages"]["routes"] == "failed"
    assert result["data"]["stages"]["explanation"] == "skipped"
    assert all(c["rank"] is None and c["route"]["value"] is None for c in cards(result))
    assert dependencies.model.calls == 0
    assert not dependencies.sessions.trips[req["trip_id"]][1].shown
    dependencies.kakao.route = AsyncMock(return_value=(600, 1000))
    recovered = send(client, next_request(req, result)).json()
    assert recovered["data"]["kind"] == "recommendation"


@pytest.mark.parametrize("seconds,kind", [(600, "recommendation"), (1801, "no_candidates")])
def test_partial_routes_preserve_stage_even_if_all_successes_over_limit(
    dependencies, client, seconds, kind
):
    async def route(c, p):
        if p.id == "1":
            return seconds, 1000
        raise ProviderError(404, "route_not_found", "no route")

    dependencies.kakao.route = route
    result = send(client, body()).json()
    assert result["data"]["kind"] == kind
    assert result["data"]["stages"]["routes"] == "partial"


def test_empty_search_and_no_result(dependencies, client):
    dependencies.kakao.search = AsyncMock(return_value=([], True))
    result = send(client, body()).json()
    assert result["data"]["kind"] == "no_candidates"
    assert result["data"]["result"] is None


def test_minute_bucket_parking_and_missing_distance(dependencies, client):
    dependencies.kakao.route = AsyncMock(
        side_effect=lambda c, p: ({"1": 659, "2": 600}.get(p.id, 2000), 500)
    )

    async def evidence(p):
        return TrafficEvidence(), ParkingEvidence(
            id="p" + p.id,
            name="주차장",
            source="seoul_getparkinfo",
            distance_m=200 if p.id == "1" else 50,
        )

    dependencies.evidence.for_place = evidence
    assert ids(send(client, body()).json()) == ["kakao:2", "kakao:1"]
    dependencies.evidence.for_place = AsyncMock(return_value=(TrafficEvidence(), None))
    assert ids(send(client, body()).json()) == ["kakao:1", "kakao:2"]


def test_duplicate_request_is_not_charged_twice_and_conflicting_body_rejected(dependencies, client):
    req = body()
    first = send(client, req)
    count = len(dependencies.kakao.routes)
    second = send(client, req)
    assert first.json() == second.json()
    assert len(dependencies.kakao.routes) == count
    req["conditions_patch"]["headcount"] = 4
    assert send(client, req).status_code == 409


def test_stale_version_rejected_without_provider_work(dependencies, client):
    req = body()
    send(client, req)
    req["request_id"] = str(uuid4())
    count = len(dependencies.kakao.routes)
    assert send(client, req).status_code == 409
    assert len(dependencies.kakao.routes) == count


def test_provider_outage_after_acceptance_retains_conditions(dependencies, client):
    dependencies.kakao.search = AsyncMock(
        side_effect=ProviderError(502, "kakao_unavailable", "unavailable")
    )
    response = send(client, body())
    assert response.status_code == 502
    result = response.json()
    assert result["data"]["kind"] == "failure"
    assert result["data"]["condition_update"]["applied"] is True
    assert result["error"]["code"] == "PROVIDER_ERROR"


def test_quota_returns_429_and_does_not_become_general_candidates(dependencies, client):
    dependencies.kakao.route = AsyncMock(side_effect=ProviderError(503, "kakao_quota", "quota"))
    response = send(client, body())
    assert response.status_code == 429
    assert response.json()["data"]["kind"] == "failure"


def test_region_is_verified_not_trusted_from_body(dependencies, client):
    dependencies.kakao.region = AsyncMock(return_value="other")
    response = send(client, body())
    assert response.status_code == 422
    assert response.json()["data"]["condition_update"]["applied"] is False
    assert not dependencies.kakao.searches


def test_seoul_destination_filter(dependencies, client):
    dependencies.kakao.region = AsyncMock(
        side_effect=lambda x, y: "seoul" if x == 126.977 else "gyeonggi"
    )
    assert send(client, body()).json()["data"]["kind"] == "no_candidates"
    assert not dependencies.kakao.routes


@pytest.mark.parametrize(
    "patch",
    [
        dict(origin=None),
        dict(purposes=None),
        dict(headcount=0),
        dict(max_travel_seconds=0),
        dict(age_groups=[]),
        dict(purposes=[dict(purpose="food"), dict(purpose="food")]),
    ],
)
def test_invalid_patches_rejected(dependencies, client, patch):
    req = body()
    req["conditions_patch"].update(patch)
    assert send(client, req).status_code == 422
    assert not dependencies.kakao.routes


def test_request_schema_and_legacy_payload_rejected(dependencies, client):
    schema = client.get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["RecommendationRequest"]
    assert set(request_schema["required"]) == {
        "request_id",
        "trip_id",
        "condition_version",
        "current_conditions",
        "excluded_place_ids",
        "conditions_patch",
    }
    assert (
        send(client, {"message": "한식집", "conditions": {"x": 127, "y": 37.5}}).status_code == 422
    )
    assert "/api/v1/chat/messages" not in schema["paths"]


def test_explanation_failure_keeps_server_result(dependencies, client):
    dependencies.model.complete = AsyncMock(
        side_effect=ProviderError(503, "openai_not_configured", "missing")
    )
    result = send(client, body()).json()
    assert result["data"]["kind"] == "recommendation"
    assert result["data"]["stages"]["explanation"] == "fallback"
    assert len(cards(result)) == 3


def test_two_purposes_search_both_without_quotas(dependencies, client):
    req = body()
    req["conditions_patch"]["purposes"].append(dict(purpose="cafe", keyword="디저트"))
    result = send(client, req).json()
    assert ("한식집", 1) in dependencies.kakao.searches and (
        "디저트",
        1,
    ) in dependencies.kakao.searches
    assert len(cards(result)) == 3


def test_server_restart_still_honors_client_exclusions(dependencies, client):
    req = body()
    first = send(client, req).json()
    follow = next_request(req, first)
    dependencies.sessions = RecommendationSessions()
    assert not set(ids(first)) & set(ids(send(client, follow).json()))


def test_missing_required_conditions_not_defaulted(dependencies, client):
    req = body()
    req["conditions_patch"] = {}
    response = send(client, req)
    assert response.status_code == 422
    assert response.json()["data"]["condition_update"]["applied"] is False


def test_excluded_list_is_not_truncated(dependencies, client):
    req = body()
    req["excluded_place_ids"] = [f"kakao:{i}" for i in range(1, 100)]
    assert send(client, req).json()["data"]["kind"] == "no_candidates"


def test_real_kakao_adapter_with_mock_http_and_structured_service():
    import asyncio

    import httpx

    from app.clients.kakao import KakaoClient
    from app.core.config import Settings
    from app.schemas.recommendations import RecommendationRequest

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "coord2regioncode" in request.url.path:
            return httpx.Response(200, json={"documents": [{"region_1depth_name": "서울특별시"}]})
        if "keyword" in request.url.path:
            assert "radius" not in request.url.params
            assert request.url.params["size"] == "15"
            return httpx.Response(
                200,
                json={
                    "meta": {"is_end": True},
                    "documents": [
                        {
                            "id": "1",
                            "place_name": "한식집",
                            "x": "127",
                            "y": "37.5",
                            "place_url": "https://place.map.kakao.com/1",
                            "category_group_code": "FD6",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"routes": [{"result_code": 0, "summary": {"duration": 1800, "distance": 12000}}]},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = KakaoClient(http, Settings(_env_file=None, kakao_rest_api_key="test-only"))
            service = StructuredRecommendationService(
                Model(), provider, Evidence(), RecommendationSessions()
            )
            status, result = await service.execute(RecommendationRequest.model_validate(body()))
            assert status == 200
            card = cards(result)[0]
            assert card["route"]["value"] == {"duration_seconds": 1800, "distance_meters": 12000}
            assert card["place"]["value"]["category_code"] == "FD6"
            assert card["place"]["collected_at"] is not None
            assert len(calls) == 4

    asyncio.run(run())


def test_concurrent_duplicate_only_executes_once():
    import asyncio

    from app.schemas.recommendations import RecommendationRequest

    async def run():
        service = StructuredRecommendationService(
            Model(), Kakao(), Evidence(), RecommendationSessions()
        )
        request = RecommendationRequest.model_validate(body())
        first, second = await asyncio.gather(service.execute(request), service.execute(request))
        assert first == second
        assert len(service.kakao.routes) == 6
        assert service.model.calls == 1

    asyncio.run(run())
