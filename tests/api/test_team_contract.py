from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def test_population_contract_retains_source_time(client):
    snapshot = SimpleNamespace(
        area_code="POI001",
        area_name="테스트 장소",
        collected_at=datetime(2026, 10, 25, tzinfo=UTC),
        data={"LIVE_PPLTN_STTS": [{"AREA_CONGEST_LVL": "여유", "PPLTN_TIME": "2026-10-25 12:00"}]},
    )
    with patch("app.api.routes.team_contract.CityDataRepository") as repository:
        repository.return_value.list_latest = AsyncMock(return_value=[snapshot])
        response = client.get("/api/v1/map/population")
    assert response.status_code == 200
    assert response.json()["scope"] == "seoul_citydata_hotspots"
    assert response.json()["items"][0]["source_time"] == "2026-10-25 12:00"


def test_population_unknown_area(client):
    with patch("app.api.routes.team_contract.CityDataRepository") as repository:
        repository.return_value.get = AsyncMock(return_value=None)
        assert client.get("/api/v1/map/population?area_code=missing").status_code == 404


def test_team_parking_maps_longitude(client):
    with patch(
        "app.api.routes.team_contract.nearby",
        new=AsyncMock(
            return_value={
                "items": [{"lon": 126.978, "lat": 37.5665}],
                "status": "registered_information",
            }
        ),
    ) as nearby:
        response = client.get("/api/v1/parkings/nearby?lat=37.5665&lng=126.978")
    assert response.status_code == 200
    assert response.json()["items"][0]["lng"] == 126.978
    assert nearby.await_args.args[2] == 126.978


def test_team_parking_validates_coordinates(client):
    assert client.get("/api/v1/parkings/nearby?lat=0&lng=0").status_code == 422


def test_traffic_preserves_geojson(client):
    payload = {"type": "FeatureCollection", "features": [], "total": 0}
    with patch("app.api.routes.team_contract.roads", new=AsyncMock(return_value=payload)):
        response = client.get("/api/v1/map/traffic?limit=1")
    assert response.json() == payload
