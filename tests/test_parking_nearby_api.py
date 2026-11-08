from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_parking_nearby_service, get_session
from app.core.config import Settings
from app.main import create_app
from app.services.parking_nearby import ParkingNearbyService

URL = "/api/v1/parking/nearby?latitude=37.5665&longitude=126.978"


@pytest.fixture
def api():
    app = create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://test:test@127.0.0.1:1/test")
    )
    repo, provider = AsyncMock(), AsyncMock()
    repo.get_version.return_value = SimpleNamespace(is_ready=True, geometry_version="v1")
    provider.region.return_value = {
        "documents": [
            {"region_type": "B", "region_1depth_name": "서울특별시"},
        ]
    }
    key = uuid4()
    repo.summarize_radius.return_value = (1, 80.5)
    repo.list_facilities.return_value = [
        (key, "unverified", {"name": "등록 주차장"}, 37.567, 126.978, 80.5)
    ]
    repo.list_records.return_value = {key: []}
    service = ParkingNearbyService(repo, provider)
    app.dependency_overrides[get_parking_nearby_service] = lambda: service

    async def session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = session
    with TestClient(app) as client:
        yield client, repo, provider, key


def test_first_radius_and_unconfirmed_registration_do_not_expand(api):
    client, repo, _, key = api
    response = client.get(URL)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["coordinates"] == {"latitude": 37.5665, "longitude": 126.978}
    summary = data["parking"]
    assert summary["search_radius_meters"] == 300
    assert summary["total_count"] == 1
    assert summary["nearest_distance_meters"] == 80.5
    assert summary["items"][0]["parking_id"] == str(key)
    assert summary["items"][0]["records"] == []
    assert summary["items"][0]["availability"]["status"] == "unsupported"
    repo.summarize_radius.assert_awaited_once()
    assert repo.summarize_radius.await_args.args[1] == 300


def test_second_radius_only_after_first_is_empty(api):
    client, repo, _, _ = api
    repo.summarize_radius.side_effect = [(0, None), (1, 450)]
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json()["data"]["parking"]["search_radius_meters"] == 500
    assert [c.args[1] for c in repo.summarize_radius.await_args_list] == [300, 500]


def test_no_records_is_not_a_system_failure(api):
    client, repo, _, _ = api
    repo.summarize_radius.return_value = (0, None)
    summary = client.get(URL).json()["data"]["parking"]
    assert summary["status"] == "unavailable"
    assert summary["reason_code"] == "NO_RECORD"
    assert summary["search_radius_meters"] == 1000
    assert summary["total_count"] == 0
    assert summary["items"] == []
    repo.list_facilities.assert_not_awaited()


def test_unprepared_inventory_has_unknown_count_and_radius(api):
    client, repo, _, _ = api
    repo.get_version.return_value = None
    response = client.get(URL)
    assert response.status_code == 200
    summary = response.json()["data"]["parking"]
    assert summary["total_count"] is None
    assert summary["search_radius_meters"] is None
    repo.summarize_radius.assert_not_awaited()


def test_limit_cannot_expand_radius_or_silently_truncate(api):
    client, repo, _, _ = api
    repo.summarize_radius.return_value = (21, 20)
    rows = [
        (uuid4(), "unverified", {"name": "등록 주차장"}, 37.567, 126.978, 20 + i) for i in range(21)
    ]
    repo.list_facilities.side_effect = lambda coordinates, radius, limit, offset: rows[
        offset : offset + limit
    ]
    response = client.get(URL)
    assert response.status_code == 200
    summary = response.json()["data"]["parking"]
    assert len(summary["items"]) == 20 and summary["total_count"] == 21
    assert summary["next_cursor"]
    repo.summarize_radius.assert_awaited_once()
    second = client.get(URL + "&cursor=" + summary["next_cursor"])
    assert second.status_code == 200
    data = second.json()["data"]["parking"]
    assert len(data["items"]) == 1 and data["next_cursor"] is None
    assert data["nearest_distance_meters"] == 20
    assert [call.args[1] for call in repo.summarize_radius.await_args_list] == [300, 300]


def test_cursor_is_explicitly_pending(api):
    client, repo, provider, _ = api
    response = client.get(URL + "&cursor=pending")
    assert response.status_code == 422
    repo.get_version.assert_not_awaited()
    provider.region.assert_not_awaited()


@pytest.mark.parametrize(
    "query",
    [
        "latitude=37.5",
        "latitude=37.5&longitude=127&lat=37.5&lon=127",
        "latitude=37.5&longitude=127&source=demo_parking",
        "latitude=NaN&longitude=127",
        "latitude=37.5&longitude=127&limit=101",
    ],
)
def test_bad_or_mixed_queries_are_contract_errors(api, query):
    client, _, provider, _ = api
    response = client.get("/api/v1/parking/nearby?" + query)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    provider.region.assert_not_awaited()


def test_supported_origin_outside_seoul_is_not_a_destination(api):
    client, repo, provider, _ = api
    provider.region.return_value["documents"][0]["region_1depth_name"] = "경기도"
    response = client.get(URL)
    assert response.status_code == 422
    repo.get_version.assert_not_awaited()


def test_db_error_is_not_successful_empty_inventory(api):
    client, repo, _, _ = api
    repo.get_version.side_effect = OperationalError("private", {}, Exception("secret"))
    response = client.get(URL)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
    assert "secret" not in response.text


def test_openapi_has_one_nearby_operation(api):
    client, _, _, _ = api
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/parking/nearby"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert set(params) == {"latitude", "longitude", "cursor", "limit"}
    assert params["latitude"]["required"] is True
    assert params["longitude"]["required"] is True
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]
