"""Public Part 1 query route wiring; no provider requests or DB connection."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.api.routes.location_queries import get_area_query_service, get_place_query_service
from app.api.routes.parking import get_parking_search_service
from app.models import ParkingSourceRecord
from app.services.area_queries import missing
from app.services.parking_search import ParkingSearchService
from app.services.query_cursors import QueryCursorStore


@pytest.fixture
def queries(app):
    places, parking, areas = AsyncMock(), AsyncMock(), AsyncMock()
    app.dependency_overrides[get_place_query_service] = lambda: places
    app.dependency_overrides[get_parking_search_service] = lambda: parking
    app.dependency_overrides[get_area_query_service] = lambda: areas
    return places, parking, areas


def test_place_and_nearby_queries_use_fact_service(client, queries):
    places = queries[0]
    places.search.return_value = {"items": [], "next_cursor": None, "has_more": False}
    response = client.get("/api/v1/locations/places/search?query=서울역")
    assert response.status_code == 200
    places.search.assert_awaited_once_with("서울역", 10, None)
    response = client.get(
        "/api/v1/locations/places/nearby?latitude=37.56&longitude=126.97&category=cafe"
    )
    assert response.status_code == 200
    assert places.search.call_args.kwargs["radius"] == 800
    assert places.search.call_args.kwargs["category"] == "CE7"


def test_manual_parking_route_precedes_uuid_route(client, queries):
    queries[1].search.return_value = {
        "checked_at": datetime.now(UTC),
        "radius_meters": 2000,
        "total_count": 0,
        "items": [],
        "next_cursor": None,
    }
    response = client.get(
        "/api/v1/parking/search?latitude=37.56&longitude=126.97&radius_meters=2000&sort=price"
    )
    assert response.status_code == 200
    request = queries[1].search.call_args.args[0]
    assert request.sort == "price" and request.radius_meters == 2000


def test_price_sort_places_conflicting_fees_after_known_prices_across_pages(client, app):
    conflict_id, free_id, paid_id, unknown_id = [UUID(int=i) for i in range(1, 5)]
    repo = AsyncMock()
    repo.get_version.return_value = SimpleNamespace(is_ready=True, geometry_version="v1")
    repo.list_facilities.return_value = [
        (key, "unverified", {"name": name}, 37.56, 126.97, meters)
        for key, name, meters in [
            (conflict_id, "요금 충돌", 10),
            (paid_id, "유료", 20),
            (free_id, "무료", 30),
            (unknown_id, "요금 미확인", 40),
        ]
    ]

    def record(paid, minutes=None, amount=None):
        return ParkingSourceRecord(
            source_record_id=uuid4(),
            source="national_parking" if paid else "seoul_getparkinfo",
            is_paid=paid,
            base_minutes=Decimal(minutes) if minutes is not None else None,
            base_fee_krw=Decimal(amount) if amount is not None else None,
            base_usable=minutes is not None and amount is not None,
        )

    repo.list_records.return_value = {
        conflict_id: [record(False), record(True, amount=1000)],
        free_id: [record(False)],
        paid_id: [record(True, 10, 500)],
        unknown_id: [],
    }
    service = ParkingSearchService(repo, cursors=QueryCursorStore())
    app.dependency_overrides[get_parking_search_service] = lambda: service
    params = {"latitude": 37.56, "longitude": 126.97, "sort": "price", "limit": 2}
    first = client.get("/api/v1/parking/search", params=params)
    assert first.status_code == 200
    page = first.json()["data"]
    assert page["total_count"] == 4 and page["next_cursor"]
    assert [item["facility"]["parking_id"] for item in page["items"]] == [
        str(free_id),
        str(paid_id),
    ]
    assert [item["hourly_base_comparison_krw"] for item in page["items"]] == [0, 3000]
    second = client.get("/api/v1/parking/search", params={**params, "cursor": page["next_cursor"]})
    assert second.status_code == 200
    page = second.json()["data"]
    assert page["next_cursor"] is None
    assert [item["facility"]["parking_id"] for item in page["items"]] == [
        str(conflict_id),
        str(unknown_id),
    ]
    conflict = page["items"][0]
    assert conflict["hourly_base_comparison_krw"] is None
    assert conflict["fee_status"] == "conflict"
    assert conflict["fee_reason"] == "CONFLICTING_SOURCE_FEES"
    records = conflict["facility"]["records"]
    assert len(records) == 2
    assert records[1]["registered"]["value"]["fees"]["base_fee_krw"] == 1000


def test_area_uses_observation_id_and_preserves_partial_response(client, queries):
    queries[2].overview.return_value = {
        "area_code": "POI001",
        "name": "관측 영역",
        "scope": "population_observation_area",
        "checked_at": datetime.now(UTC),
        **{key: missing() for key in ("population", "roads", "parking", "weather", "commerce")},
    }
    response = client.get("/api/v1/locations/areas/POI001/overview")
    assert response.status_code == 200
    assert response.json()["data"]["scope"] == "population_observation_area"
    queries[2].overview.assert_awaited_once_with("POI001", include_extras=False)


def test_driving_passes_wgs84_coordinates(client, queries):
    queries[0].driving.return_value = {
        "origin": {"latitude": 37.56, "longitude": 126.97},
        "destination": {"latitude": 37.57, "longitude": 126.98},
        "checked_at": datetime.now(UTC),
        "route": missing(),
    }
    response = client.get(
        "/api/v1/locations/routes/driving?origin_latitude=37.56&origin_longitude=126.97&destination_latitude=37.57&destination_longitude=126.98&include_geometry=false"
    )
    assert response.status_code == 200
    args = queries[0].driving.call_args
    assert args.args[0].longitude == 126.97
    assert args.args[1].latitude == 37.57
    assert args.kwargs["include_geometry"] is False


@pytest.mark.parametrize(
    "url",
    [
        "/locations/places/search?query=",
        "/locations/places/nearby?latitude=37&longitude=127&category=invalid",
        "/locations/places/nearby?latitude=NaN&longitude=127&category=cafe",
        "/parking/search?latitude=37&longitude=127&radius_meters=300",
        "/parking/search?latitude=37&longitude=127&sort=vacancy",
        "/locations/routes/driving?origin_latitude=91",
    ],
)
def test_bad_queries_do_not_call_services(client, queries, url):
    response = client.get("/api/v1" + url)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    for service in queries:
        for call in ("search", "overview", "driving"):
            getattr(service, call).assert_not_awaited()
