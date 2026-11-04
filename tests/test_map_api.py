import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_map_service
from app.core.config import Settings
from app.main import create_app
from app.repositories.map import SelectedFeature
from app.services.map import MapService, information

QUERY = "bbox=126.95,37.54,127.03,37.59&level=6"


@pytest.fixture
def api():
    app = create_app(
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/test",
        )
    )
    repository = AsyncMock()
    repository.get_version.return_value = SimpleNamespace(
        is_ready=True,
        geometry_version="v1",
        coverage="Stored provider data only",
    )
    repository.select_features.return_value = []
    repository.get_observations.return_value = {}
    app.dependency_overrides[get_map_service] = lambda: MapService(repository, max_features=2)
    with TestClient(app) as client:
        yield client, repository


def feature(layer, key="a", shape=None, **properties):
    props = {
        "roads": {"link_id": key, "name": None, "direction": None},
        "parking": {"parking_id": key, "name": "테스트", "identity_status": "unverified"},
        "population": {"area_code": key, "name": "관측 영역"},
    }[layer] | properties
    return SelectedFeature(
        key,
        props,
        json.dumps(
            shape
            or {
                "type": "LineString",
                "coordinates": [[126.97, 37.55], [127, 37.56]],
            }
        ),
    )


@pytest.mark.parametrize(
    "layer,shape",
    [
        ("roads", {"type": "MultiLineString", "coordinates": [[[126.97, 37.55], [127, 37.56]]]}),
        ("parking", {"type": "Point", "coordinates": [126.97, 37.55]}),
        (
            "population",
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [
                            [126.95, 37.54],
                            [127.03, 37.54],
                            [127.03, 37.59],
                            [126.95, 37.59],
                            [126.95, 37.54],
                        ],
                        [
                            [126.97, 37.55],
                            [126.98, 37.55],
                            [126.98, 37.56],
                            [126.97, 37.56],
                            [126.97, 37.55],
                        ],
                    ]
                ],
            },
        ),
    ],
)
def test_geometry_preserves_layer_shapes_and_holes(api, layer, shape):
    client, repository = api
    key = str(uuid4()) if layer == "parking" else "a"
    repository.select_features.return_value = [feature(layer, key, shape)]
    response = client.get(f"/api/v1/map/layers/{layer}/geometry?{QUERY}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bbox"] == [126.95, 37.54, 127.03, 37.59]
    actual = data["feature_collection"]["features"][0]
    assert actual["id"] == key
    assert actual["geometry"] == shape
    assert not ({"traffic", "population", "fees"} & actual["properties"].keys())


@pytest.mark.parametrize("bbox", ["nan,0,1,2", "0,0,inf,1", "1,0,0,1", "0,-91,1,1", "1,2,3"])
def test_bad_bbox_is_contract_error(api, bbox):
    client, repository = api
    response = client.get(f"/api/v1/map/layers/roads/geometry?bbox={bbox}&level=6")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_MAP_QUERY"
    repository.get_version.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        f"/map/layers/sensors/geometry?{QUERY}",
        f"/map/layers/parking/status?{QUERY}&geometry_version=v1",
        "/map/layers/roads/geometry?bbox=0,0,1,1&level=0",
    ],
)
def test_layer_and_level_validation(api, path):
    client, _ = api
    response = client.get("/api/v1" + path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_empty_ready_range_is_success(api):
    client, _ = api
    response = client.get(f"/api/v1/map/layers/roads/status?{QUERY}&geometry_version=v1")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert response.json()["data"]["status"] == "ok"


def test_unready_layer_and_version_mismatch(api):
    client, repository = api
    url = f"/api/v1/map/layers/roads/status?{QUERY}&geometry_version=old"
    response = client.get(url)
    assert response.status_code == 409
    repository.select_features.assert_not_awaited()
    repository.get_version.return_value.is_ready = False
    assert client.get(url).status_code == 503


def test_range_is_not_silently_truncated(api):
    client, repository = api
    repository.select_features.return_value = [feature("roads", key) for key in ("a", "b", "c")]
    response = client.get(f"/api/v1/map/layers/roads/geometry?{QUERY}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MAP_RANGE_TOO_LARGE"


def test_status_matches_geometry_ids_without_resending_shapes(api):
    client, repository = api
    repository.select_features.return_value = [feature("roads", key) for key in ("a", "b")]
    repository.get_observations.return_value = {
        "a": SimpleNamespace(
            value={"speed_kmh": 0, "travel_seconds": 25},
            observed_at=None,
            collected_at=datetime.now(UTC),
            collection_failed=False,
        )
    }
    geometry = client.get(f"/api/v1/map/layers/roads/geometry?{QUERY}").json()["data"]
    response = client.get(f"/api/v1/map/layers/roads/status?{QUERY}&geometry_version=v1")
    data = response.json()["data"]
    assert [f["id"] for f in geometry["feature_collection"]["features"]] == [
        item["link_id"] for item in data["items"]
    ]
    assert data["items"][0]["traffic"]["value"]["speed_kmh"] == 0
    assert data["items"][1]["traffic"]["reason_code"] == "NO_RECORD"
    assert data["status"] == "partial"
    assert all(set(item) == {"link_id", "traffic"} for item in data["items"])
    assert repository.select_features.await_args.kwargs["include_geometry"] is False


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (OperationalError("secret-db", {}, Exception("secret")), 503, "DATA_UNAVAILABLE"),
        (RuntimeError("secret-key"), 500, "INTERNAL_ERROR"),
    ],
)
def test_errors_are_safe_common_envelopes(api, failure, status, code):
    client, repository = api
    repository.get_version.side_effect = failure
    response = client.get(f"/api/v1/map/layers/roads/geometry?{QUERY}")
    assert response.status_code == status
    assert response.json()["data"] is None
    assert response.json()["error"]["code"] == code
    assert "secret" not in response.text


def test_bad_stored_geometry_and_mismatched_id_is_503(api):
    client, repository = api
    repository.select_features.return_value = [feature("roads", link_id="wrong")]
    assert client.get(f"/api/v1/map/layers/roads/geometry?{QUERY}").status_code == 503


@pytest.mark.parametrize(
    "layer,minutes,freshness,available",
    [
        ("roads", 30, "unknown", True),
        ("roads", 30.01, "unknown", False),
        ("population", 60, "fresh", True),
        ("population", 120, "stale", True),
        ("population", 120.01, "stale", False),
    ],
)
def test_freshness_boundaries(layer, minutes, freshness, available):
    now = datetime.now(UTC)
    row = SimpleNamespace(
        value={"speed_kmh": 0, "travel_seconds": 20}
        if layer == "roads"
        else {
            "min_people": 0,
            "max_people": 10,
            "congestion": "normal",
            "is_replacement": None,
        },
        collected_at=now - timedelta(minutes=minutes),
        observed_at=None if layer == "roads" else now - timedelta(minutes=minutes),
        collection_failed=True,
    )
    result = information(layer, row, now)
    assert result["freshness"] == freshness
    assert (result["value"] is not None) == available
    assert result["reason_code"] == ("COLLECTION_FAILED" if available else "STALE_DATA")


def test_bad_measurement_does_not_discard_other_valid_metric(api):
    client, repository = api
    repository.select_features.return_value = [feature("roads")]
    repository.get_observations.return_value = {
        "a": SimpleNamespace(
            value={"speed_kmh": -1, "travel_seconds": 0},
            observed_at=None,
            collected_at=datetime.now(UTC),
            collection_failed=False,
        )
    }
    data = client.get(f"/api/v1/map/layers/roads/status?{QUERY}&geometry_version=v1").json()["data"]
    assert data["status"] == "partial"
    assert data["items"][0]["traffic"]["value"] == {"speed_kmh": None, "travel_seconds": 0}


def test_replacement_future_and_missing_time_are_unavailable():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        value={"min_people": 5, "is_replacement": True},
        collected_at=now,
        observed_at=now,
        collection_failed=False,
    )
    assert information("population", row, now)["reason_code"] == "REPLACEMENT_VALUE"
    row.observed_at = now + timedelta(minutes=1)
    result = information("population", row, now)
    assert result["reason_code"] == "SOURCE_TIME_UNKNOWN"
    assert result["observed_at"] is None


def test_openapi_documents_each_layer_and_error(api):
    client, _ = api
    schema = client.get("/openapi.json").json()
    assert {
        "RoadGeometry",
        "ParkingGeometry",
        "PopulationGeometry",
        "RoadStatus",
        "PopulationStatus",
        "ApiError",
    } <= schema["components"]["schemas"].keys()
    operation = schema["paths"]["/api/v1/map/layers/{layer}/status"]["get"]
    assert {"200", "409", "422", "500", "503"} <= operation["responses"].keys()
