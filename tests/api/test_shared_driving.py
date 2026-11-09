"""Exercise shared driving dependencies with HTTP transport mocked, no live calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.api.dependencies import (
    get_ai_provider,
    get_http_client,
    get_parking_nearby_service,
    get_recommendation_execution,
)
from app.modules.recommendation.cards import unavailable_parking
from app.modules.recommendation.progress import MemoryProgressStore
from app.services.recommendation_execution import RecommendationExecution
from tests.unit.recommendation.test_progress import request
from tests.unit.services.test_part1_queries import route_body

DRIVING = "/api/v1/locations/routes/driving"
R01 = "/api/v1/recommendations"
PARAMS = {
    "origin_latitude": 37.56,
    "origin_longitude": 126.97,
    "destination_latitude": 37.57,
    "destination_longitude": 126.98,
}


@pytest.fixture
def driving_provider(app, geography):
    state = SimpleNamespace(mode="ok", calls=[])

    def respond(req):
        state.calls.append(req)
        if req.url.path.endswith("coord2regioncode.json"):
            return httpx.Response(
                200, json={"documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]}
            )
        if req.url.path.endswith("keyword.json"):
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "id": "shared",
                            "place_name": "공통 경로 카페",
                            "category_group_code": "CE7",
                            "x": "126.98",
                            "y": "37.57",
                        }
                    ],
                    "meta": {"is_end": True},
                },
            )
        assert req.url.host == "apis-navi.kakaomobility.com"
        assert req.url.path == "/v1/directions"
        if isinstance(state.mode, int):
            return httpx.Response(
                state.mode, json={"private": "provider-private"}, headers={"Retry-After": "8"}
            )
        if state.mode == "timeout":
            raise httpx.ReadTimeout("provider-private", request=req)
        if state.mode == "invalid-json":
            return httpx.Response(200, text="provider-private")
        if state.mode == "malformed":
            return httpx.Response(200, json=route_body(summary={"duration": True, "distance": 1}))
        if state.mode == "no-route":
            return httpx.Response(200, json={"routes": [{"result_code": 1}]})
        return httpx.Response(200, json=route_body())

    async def http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            yield client

    app.state.settings.kakao_rest_api_key = SecretStr("test-key")
    parking = AsyncMock()
    parking.search.return_value = SimpleNamespace(parking=unavailable_parking())
    execution = RecommendationExecution(MemoryProgressStore())
    app.dependency_overrides[get_http_client] = http
    app.dependency_overrides[get_parking_nearby_service] = lambda: parking
    app.dependency_overrides[get_ai_provider] = lambda: None
    app.dependency_overrides[get_recommendation_execution] = lambda: execution
    return state


def test_independent_and_recommendation_share_route_values_and_geometry_option(
    client, driving_provider
):
    direct = client.get(DRIVING, params=PARAMS)
    assert direct.status_code == 200
    route = direct.json()["data"]["route"]
    assert route["value"]["geometry"]["coordinates"][0][0] == [126.978, 37.5665]
    assert route["value"]["taxi_fare_krw"] is None
    assert route["value"]["toll_fare_krw"] is None
    assert route["observed_at"] is None and route["collected_at"]
    body = request().model_dump(mode="json", exclude_unset=True)
    response = client.post(R01, json=body)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "recommendation"
    assert data["result"]["cards"][0]["route"]["value"] == {
        "duration_seconds": route["value"]["duration_seconds"],
        "distance_meters": route["value"]["distance_meters"],
    }
    calls = [c for c in driving_provider.calls if c.url.path == "/v1/directions"]
    assert len(calls) == 1  # The recent full route also supplies the recommendation ETA.
    assert calls[0].url.params["summary"] == "false"
    assert data["result"]["cards"][0]["route"]["collected_at"] == route["collected_at"]
    for call in calls:
        assert call.url.params["origin"] == "126.97,37.56"
        assert call.url.params["destination"] == "126.98,37.57"
        assert call.url.params["priority"] == "RECOMMEND"
    before = len(driving_provider.calls)
    replay = client.post(R01, json=body)
    assert replay.status_code == 200
    # The envelope's server_time is generated for each HTTP response.
    for key in ("request_id", "data", "error"):
        assert replay.json()[key] == response.json()[key]
    assert len(driving_provider.calls) == before


@pytest.mark.parametrize(
    "mode,status,code,fatal",
    [
        (401, 503, "SERVICE_UNAVAILABLE", True),
        (403, 503, "SERVICE_UNAVAILABLE", True),
        (429, 429, "RATE_LIMITED", True),
        (503, 502, "PROVIDER_ERROR", False),
        ("timeout", 504, "PROVIDER_TIMEOUT", False),
        ("invalid-json", 502, "PROVIDER_ERROR", False),
        ("malformed", 502, "PROVIDER_ERROR", False),
        ("no-route", 404, "NOT_FOUND", False),
    ],
)
def test_route_failure_policy_at_each_entry_point(
    client, driving_provider, mode, status, code, fatal
):
    driving_provider.mode = mode
    direct = client.get(DRIVING, params=PARAMS)
    assert direct.status_code == status
    assert direct.json()["error"]["code"] == code
    recommendation = client.post(R01, json=request().model_dump(mode="json", exclude_unset=True))
    assert recommendation.status_code == (status if fatal else 200)
    data = recommendation.json()["data"]
    if fatal:
        assert data["kind"] == "failure" and data["result"] is None
        assert data["condition_update"]["applied"] is True
        assert recommendation.json()["error"]["code"] == code
        if mode in (401, 403):
            assert direct.json()["error"]["retryable"] is False
            assert recommendation.json()["error"]["retryable"] is False
        else:
            assert direct.headers["retry-after"] == recommendation.headers["retry-after"] == "8"
    else:
        assert data["kind"] == "general_candidates"
        assert data["result"]["cards"][0]["route"]["value"] is None
        assert data["result"]["cards"][0]["rank"] is None
    for response in (direct, recommendation):
        assert "provider-private" not in response.text and "test-key" not in response.text


def test_missing_key_is_nonretryable_and_never_calls_provider(client, app, driving_provider):
    app.state.settings.kakao_rest_api_key = SecretStr("")
    for response in (
        client.get(DRIVING, params=PARAMS),
        client.post(R01, json=request().model_dump(mode="json", exclude_unset=True)),
    ):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert response.json()["error"]["retryable"] is False
    assert driving_provider.calls == []
