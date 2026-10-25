from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.routes.supplemental import cctv_item, sensor_status
from jobs.collect_supplemental import latest_sensors


def test_sdot_keeps_zero_and_excludes_old_counts():
    now = datetime.now(UTC)
    item = {"id": "1", "count_10min": 0, "observed_at": now.isoformat()}
    assert sensor_status(item, now)["count_10min"] == 0
    assert sensor_status(item, now + timedelta(hours=1))["count_10min"] is None
    assert sensor_status(item, now - timedelta(hours=1))["count_10min"] is None


def test_sdot_normalizes_codes_and_rejects_conflicting_count():
    locations = {"3011": {"id": "3011", "lat": 37.5, "lng": 127}}
    rows = [
        {"SERIAL": "00000003011", "SENSING_TIME": "2026-10-25_18:00:00", "VISITOR_COUNT": str(n)}
        for n in (1, 2)
    ]
    item = latest_sensors(locations, rows)[0]
    assert item["observation_status"] == "conflicting"
    assert item["count_10min"] is None


def test_cctv_expiry_and_unsafe_url():
    now = datetime.now(UTC)
    item = {"url": "https://example.com/video.m3u8"}
    assert cctv_item(item, now.isoformat(), now)["url"] == item["url"]
    assert cctv_item(item, (now - timedelta(hours=24)).isoformat(), now)["url"] is None
    assert cctv_item({"url": "javascript:alert(1)"}, now.isoformat(), now)["url"] is None


def test_read_routes_use_saved_snapshot_only(client):
    record = SimpleNamespace(
        data={"items": [], "issued_at": datetime.now(UTC).isoformat()},
        collected_at=datetime.now(UTC),
    )
    with patch("app.api.routes.supplemental.snapshot", new=AsyncMock(return_value=record)):
        assert client.get("/api/v1/map/cctv").status_code == 200
        assert client.get("/api/v1/map/pedestrians").status_code == 200


def test_areas_filter_district(client):
    record = SimpleNamespace(
        data={
            "features": [
                {"properties": {"district": "강남구"}},
                {"properties": {"district": "중구"}},
            ]
        },
        collected_at=datetime.now(UTC),
    )
    with patch("app.api.routes.supplemental.snapshot", new=AsyncMock(return_value=record)):
        response = client.get("/api/v1/map/areas?district=중구")
    assert len(response.json()["features"]) == 1


def test_population_geometry_excludes_replacement(client):

    from app.repositories.citydata import CityDataRepository

    geometry = SimpleNamespace(
        data={
            "features": [
                {
                    "id": "POI001",
                    "properties": {"name": "test"},
                    "geometry": {"type": "Polygon", "coordinates": []},
                }
            ]
        },
        collected_at=datetime.now(UTC),
    )
    observation = SimpleNamespace(
        area_code="POI001",
        collected_at=datetime.now(UTC),
        data={
            "LIVE_PPLTN_STTS": [
                {
                    "REPLACE_YN": "Y",
                    "AREA_CONGEST_LVL": "여유",
                    "PPLTN_TIME": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    with (
        patch("app.api.routes.supplemental.snapshot", new=AsyncMock(return_value=geometry)),
        patch.object(CityDataRepository, "list_latest", new=AsyncMock(return_value=[observation])),
    ):
        response = client.get("/api/v1/map/population/geometry")
    assert response.status_code == 200
    props = response.json()["features"][0]["properties"]
    assert props["status"] == "replacement"
    assert props["congestion"] is None


def test_official_place_file_has_unique_121_codes():
    from pathlib import Path

    from jobs.import_place_boundaries import read_shapes

    data = read_shapes(
        Path(__file__).resolve().parents[2] / "jobs/data/citydata_places_official.zip"
    )
    assert len(data) == 121
    assert len({row[0]["AREA_CD"] for row in data}) == 121
    assert all(ring[0] == ring[-1] for _, rings in data for ring in rings)
