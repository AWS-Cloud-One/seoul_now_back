from unittest.mock import AsyncMock

from app.api.dependencies import (
    get_parking_nearby_service,
)


def test_openapi_exposes_only_product_contracts(app):
    schema = app.openapi()
    assert set(schema["paths"]) == {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/map/layers/{layer}/geometry",
        "/api/v1/map/layers/{layer}/status",
        "/api/v1/locations/search",
        "/api/v1/locations/resolve",
        "/api/v1/parking/nearby",
        "/api/v1/parking/{parking_id}",
        "/api/v1/recommendations",
        "/api/v1/chat/messages",
        "/api/v1/recommendations/retry-routes",
        "/api/v1/places/{place_id}",
        "/api/v1/places/{place_id}/travel-time",
        "/api/v1/data-status",
    }
    models = schema["components"]["schemas"]
    assert {"RecommendationResponse", "ParkingFacility", "ParkingSourceRecord"} <= models.keys()
    assert not {"RecommendationData", "NearbyFacility", "ParkingSourceResponse"} & models.keys()


def test_product_nearby_rejects_an_invalid_service_response(app, client):
    service = AsyncMock()
    service.search.return_value = {"unexpected": "not a P02 response"}
    app.dependency_overrides[get_parking_nearby_service] = lambda: service
    response = client.get("/api/v1/parking/nearby?latitude=37.56&longitude=126.97")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["data"] is None


def test_removed_development_routes_are_not_registered(client):
    for path in (
        "/kakao/places",
        "/kakao/directions",
        "/map/traffic",
        "/map/population",
        "/map/pedestrians",
        "/map/cctv",
        "/collections/runs",
        "/traffic/network",
        "/citydata/areas",
        "/citydata/inspect",
        "/parkings/nearby",
        "/parking/municipal/raw",
    ):
        assert client.get("/api/v1" + path).status_code == 404


def test_nearby_requires_spec_coordinates_without_legacy_fallback(app, client):
    service = AsyncMock()
    app.dependency_overrides[get_parking_nearby_service] = lambda: service
    for query in ("", "lat=37.56&lon=126.97", "lat=37.56&lng=126.97"):
        response = client.get("/api/v1/parking/nearby?" + query)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        service.search.assert_not_awaited()
