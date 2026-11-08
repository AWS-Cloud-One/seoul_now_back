"""Public Part 1 query route wiring; no provider requests or DB connection."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.api.routes.location_queries import get_area_query_service, get_place_query_service
from app.api.routes.parking import get_parking_search_service
from app.services.area_queries import missing


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
