import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_kakao_local_client
from app.clients.kakao import KakaoError, KakaoLocalClient
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def api():
    app = create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://test:test@127.0.0.1:1/test")
    )
    provider = AsyncMock()
    provider.region.return_value = {
        "documents": [
            {"region_type": "B", "region_1depth_name": "서울특별시", "x": 127, "y": 38},
        ]
    }
    provider.coordinate_address.return_value = {
        "documents": [
            {
                "road_address": {"address_name": "서울 중구 세종대로 110"},
                "address": {"address_name": "서울 중구 태평로1가 31"},
            }
        ]
    }
    app.dependency_overrides[get_kakao_local_client] = lambda: provider
    with TestClient(app) as client:
        yield client, provider


@pytest.mark.parametrize(
    "province,region,supported",
    [
        ("서울특별시", "seoul", True),
        ("인천광역시", "incheon", True),
        ("경기도", "gyeonggi", True),
        ("부산광역시", "other", False),
    ],
)
def test_resolve_preserves_input_point_and_uses_confirmed_region(api, province, region, supported):
    client, provider = api
    provider.region.return_value["documents"][0]["region_1depth_name"] = province
    response = client.get("/api/v1/locations/resolve?latitude=37.5665&longitude=126.978")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] and body["server_time"]
    assert body["error"] is None
    location = body["data"]["location"]
    assert location["coordinates"] == {"latitude": 37.5665, "longitude": 126.978}
    assert location["label"] == location["address"] == "서울 중구 세종대로 110"
    assert location["region"] == region
    assert location["supported"] is supported
    provider.region.assert_awaited_once_with(37.5665, 126.978)
    provider.coordinate_address.assert_awaited_once_with(37.5665, 126.978)
    provider.keyword.assert_not_awaited()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"latitude": 37},
        {"longitude": 127},
        {"latitude": 91, "longitude": 127},
        {"latitude": -91, "longitude": 127},
        {"latitude": 37, "longitude": 181},
        {"latitude": 37, "longitude": -181},
        {"latitude": "NaN", "longitude": 127},
        {"latitude": 37, "longitude": "Infinity"},
        {"latitude": "text", "longitude": 127},
    ],
)
def test_invalid_coordinates_do_not_call_provider(api, params):
    client, provider = api
    response = client.get("/api/v1/locations/resolve", params=params)
    assert response.status_code == 422
    assert response.json()["data"] is None
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    provider.region.assert_not_awaited()
    provider.coordinate_address.assert_not_awaited()


def test_worldwide_coordinate_range_is_not_restricted_to_seoul(api):
    client, provider = api
    provider.region.return_value = {"documents": []}
    response = client.get("/api/v1/locations/resolve?latitude=-90&longitude=180")
    assert response.status_code == 502
    provider.region.assert_awaited_once_with(-90.0, 180.0)
    provider.coordinate_address.assert_not_awaited()


def test_land_address_fallback(api):
    client, provider = api
    provider.coordinate_address.return_value["documents"][0]["road_address"] = None
    location = client.get("/api/v1/locations/resolve?latitude=37.5665&longitude=126.978").json()[
        "data"
    ]["location"]
    assert location["address"] == "서울 중구 태평로1가 31"


@pytest.mark.parametrize(
    "body",
    [
        {"documents": []},
        {"documents": [{}]},
        {"documents": [None]},
        {"documents": [{"road_address": "bad"}]},
        {
            "documents": [
                {"address": {"address_name": "住所A"}},
                {"address": {"address_name": "住所B"}},
            ]
        },
    ],
)
def test_missing_or_conflicting_address_is_not_success(api, body):
    client, provider = api
    provider.coordinate_address.return_value = body
    response = client.get("/api/v1/locations/resolve?latitude=37.5665&longitude=126.978")
    assert response.status_code == 502
    assert response.json()["data"] is None
    assert response.json()["error"]["code"] == "PROVIDER_ERROR"


@pytest.mark.parametrize("operation", ["region", "coordinate_address"])
@pytest.mark.parametrize(
    "status,code",
    [
        (429, "RATE_LIMITED"),
        (502, "PROVIDER_ERROR"),
        (503, "SERVICE_UNAVAILABLE"),
        (504, "PROVIDER_TIMEOUT"),
    ],
)
def test_each_provider_error_is_mapped_safely(api, operation, status, code):
    client, provider = api
    getattr(provider, operation).side_effect = KakaoError(
        status,
        "안전한 오류 안내",
        7 if status == 429 else None,
    )
    response = client.get("/api/v1/locations/resolve?latitude=37.5665&longitude=126.978")
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert (response.headers.get("retry-after") == "7") == (status == 429)
    assert error["retry_after_seconds"] == (7 if status == 429 else None)
    getattr(provider, operation).assert_awaited_once()


def test_coordinate_address_transport_order():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"documents": []})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await KakaoLocalClient(http, "test-key").coordinate_address(37.5665, 126.978)

    asyncio.run(check())
    assert len(calls) == 1
    assert calls[0].url.path == "/v2/local/geo/coord2address.json"
    assert calls[0].url.params["x"] == "126.978"
    assert calls[0].url.params["y"] == "37.5665"
    assert calls[0].url.params["input_coord"] == "WGS84"


def test_resolve_openapi(api):
    client, _ = api
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/locations/resolve"]["get"]
    assert operation["tags"] == ["locations"]
    assert {"200", "422", "429", "502", "503", "504"} <= operation["responses"].keys()
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["latitude"]["required"] and params["longitude"]["required"]
