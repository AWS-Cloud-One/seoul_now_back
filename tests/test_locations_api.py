import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_location_search_service
from app.clients.kakao import KakaoError, kakao_get
from app.core.config import Settings
from app.main import create_app
from app.services.locations import LocationSearchService, SearchPage


def page(rows=None, is_end=True):
    return {"documents": rows or [], "meta": {"is_end": is_end}}


def candidate(**values):
    return {
        "place_name": "서울역",
        "x": "126.9706",
        "y": "37.5547",
        "address_name": "서울 중구 봉래동2가",
        "road_address_name": "서울 중구 한강대로 405",
    } | values


def region(name="서울특별시"):
    return {"documents": [{"region_type": "B", "region_1depth_name": name}]}


@pytest.fixture
def api():
    app = create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://test:test@127.0.0.1:1/test")
    )
    provider = AsyncMock()
    provider.keyword.return_value = page([candidate()])
    provider.address.return_value = page()
    provider.region.return_value = region()
    service = LocationSearchService(provider)
    app.dependency_overrides[get_location_search_service] = lambda: service
    with TestClient(app) as client:
        yield client, provider, service


@pytest.mark.parametrize(
    "province,expected,supported",
    [
        ("서울특별시", "seoul", True),
        ("인천광역시", "incheon", True),
        ("경기도", "gyeonggi", True),
        ("부산광역시", "other", False),
    ],
)
def test_location_normalization_and_coordinate_based_support(api, province, expected, supported):
    client, provider, _ = api
    provider.region.return_value = region(province)
    # The display address says Seoul; region must come from the coordinate lookup.
    response = client.get("/api/v1/locations/search", params={"query": " 서울역 "})
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["error"] is None
    assert body["data"]["next_cursor"] is None
    item = body["data"]["items"][0]
    assert item["coordinates"] == {"latitude": 37.5547, "longitude": 126.9706}
    assert item["region"] == expected
    assert item["supported"] is supported
    provider.region.assert_awaited_once_with(37.5547, 126.9706)
    provider.keyword.assert_awaited_once_with("서울역", page=1, size=11)


def test_address_fallback_and_no_results(api):
    client, provider, _ = api
    provider.keyword.return_value = page()
    row = candidate()
    del row["place_name"]
    provider.address.return_value = page([row])
    response = client.get("/api/v1/locations/search?query=서울시청")
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["label"] == row["address_name"]
    provider.address.return_value = page()
    provider.region.reset_mock()
    response = client.get("/api/v1/locations/search?query=없는주소")
    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "next_cursor": None}
    provider.region.assert_not_awaited()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"query": " "},
        {"query": "a" * 101},
        {"query": "서울", "limit": 0},
        {"query": "서울", "limit": 21},
        {"query": "서울", "limit": "bad"},
    ],
)
def test_query_validation(api, params):
    client, provider, _ = api
    response = client.get("/api/v1/locations/search", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    provider.keyword.assert_not_awaited()


def test_more_results_are_not_silently_marked_as_last_page(api):
    client, provider, _ = api
    provider.keyword.return_value = page([candidate()], is_end=False)
    response = client.get("/api/v1/locations/search?query=카페")
    assert response.status_code == 503
    assert response.json()["data"] is None
    assert response.json()["error"]["retryable"] is False
    provider.region.assert_not_awaited()


def test_supplied_cursor_is_unavailable_without_a_policy(api):
    client, provider, _ = api
    response = client.get("/api/v1/locations/search?query=서울&cursor=opaque-token")
    assert response.status_code == 503
    provider.keyword.assert_not_awaited()


def test_team_pagination_adapter_can_forward_a_cursor(api):
    client, _, service = api
    service.pagination = AsyncMock()
    service.pagination.search.return_value = SearchPage([candidate()], True, "next-team-cursor")
    response = client.get("/api/v1/locations/search?query=서울&cursor=previous&limit=2")
    assert response.status_code == 200
    assert response.json()["data"]["next_cursor"] == "next-team-cursor"
    assert service.pagination.search.await_args.args[1:] == ("서울", "previous", 2)


@pytest.mark.parametrize(
    "bad_region",
    [
        {"documents": []},
        region(""),
        region("알수없는지역"),
        {
            "documents": [
                {"region_type": "B", "region_1depth_name": "서울특별시"},
                {"region_type": "H", "region_1depth_name": "경기도"},
            ]
        },
    ],
)
def test_unknown_region_is_a_failure_not_outside_service(api, bad_region):
    client, provider, _ = api
    provider.region.return_value = bad_region
    response = client.get("/api/v1/locations/search?query=서울역")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PROVIDER_ERROR"


def test_invalid_coordinates_fail_without_making_region_request(api):
    client, provider, _ = api
    provider.keyword.return_value = page([candidate(x="NaN")])
    assert client.get("/api/v1/locations/search?query=서울역").status_code == 502
    provider.region.assert_not_awaited()


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "RATE_LIMITED"),
        (502, "PROVIDER_ERROR"),
        (503, "SERVICE_UNAVAILABLE"),
        (504, "PROVIDER_TIMEOUT"),
    ],
)
def test_provider_errors_and_retry_after(api, status, code):
    client, provider, _ = api
    provider.keyword.side_effect = KakaoError(status, "안전한 오류", 30 if status == 429 else None)
    response = client.get("/api/v1/locations/search?query=서울역")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert ("retry-after" in response.headers) == (status == 429)
    provider.keyword.assert_awaited_once()


def test_limit_twenty_collects_both_provider_pages_without_losing_items(api):
    client, provider, _ = api
    provider.keyword.side_effect = [
        page([candidate() for _ in range(15)], False),
        page([candidate() for _ in range(5)]),
    ]
    response = client.get("/api/v1/locations/search?query=검색&limit=20")
    assert response.status_code == 200
    assert len(response.json()["data"]["items"]) == 20
    assert provider.keyword.await_count == 2
    # Identical coordinates reuse a region lookup only within this request.
    provider.region.assert_awaited_once()


@pytest.mark.parametrize("status,expected", [(401, 502), (403, 502), (429, 429), (500, 502)])
def test_shared_transport_does_not_leak_key_or_response(status, expected):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, json={"secret": "provider-private"}, headers={"Retry-After": "7"}
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(KakaoError) as error:
                await kakao_get(client, "https://dapi.kakao.com/test", {}, "secret-api-key")
        assert error.value.status == expected
        assert "secret-api-key" not in str(error.value)
        assert "provider-private" not in str(error.value)
        assert len(calls) == 1

    asyncio.run(check())


def test_openapi_and_existing_kakao_route(api):
    client, _, _ = api
    schema = client.get("/openapi.json").json()
    assert "/api/v1/kakao/places" not in schema["paths"]
    operation = schema["paths"]["/api/v1/locations/search"]["get"]
    assert operation["tags"] == ["locations"]
    assert {"200", "422", "429", "502", "503", "504"} <= operation["responses"].keys()


def test_missing_key_does_not_send_a_request():
    calls = []

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: calls.append(request),
            )
        ) as client:
            with pytest.raises(KakaoError) as error:
                await kakao_get(client, "https://dapi.kakao.com/test", {}, " ")
        assert error.value.status == 503
        assert calls == []

    asyncio.run(check())


def test_timeout_is_504_without_automatic_retry():
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("private exception", request=request)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(KakaoError) as error:
                await kakao_get(client, "https://dapi.kakao.com/test", {}, "test-key")
        assert error.value.status == 504
        assert "private" not in str(error.value)
        assert len(calls) == 1

    asyncio.run(check())
