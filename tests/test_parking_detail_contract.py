"""P04 contract tests, independent of collector data and provider credentials."""

import warnings
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_session
from app.core.config import Settings
from app.main import create_app
from app.models import ParkingSourceRecord


@pytest.fixture
def detail_client():
    app = create_app(
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/test",
            docs_enabled=True,
        )
    )
    session = AsyncMock()
    parking_id = uuid4()
    session.scalar.return_value = SimpleNamespace(
        parking_id=parking_id,
        is_active=True,
        identity_status="unverified",
    )
    session.scalars.return_value = []

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client, session, parking_id


def test_registered_detail_preserves_source_values(detail_client):
    client, session, parking_id = detail_client
    record_id = uuid4()
    session.scalars.return_value = [
        ParkingSourceRecord(
            source_record_id=record_id,
            source="seoul_getparkinfo",
            reference_date="2026-10-22",
            address="서울",
            registered_capacity=0,
            weekday_hours="0000~0000",
            collected_at=datetime(2026, 10, 22, 5, tzinfo=UTC),
            reason_code="SOURCE_TIME_UNKNOWN",
        )
    ]
    response = client.get(f"/api/v1/parking/{parking_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["request_id"]
    data = body["data"]
    assert data["parking_id"] == str(parking_id)
    assert data["availability"]["status"] == "unsupported"
    assert data["availability"]["value"] is None
    record = data["records"][0]
    assert record["source_record_id"] == str(record_id)
    assert record["reference_date"] == "2026-10-22"
    assert record["registered"]["value"]["registered_capacity"] == 0
    assert record["registered"]["value"]["fees"] is None
    assert record["registered"]["value"]["operating_hours"]["weekdays"] == "0000~0000"
    assert record["registered"]["collected_at"] == "2026-10-22T05:00:00Z"
    assert datetime.fromisoformat(data["checked_at"]).tzinfo == UTC


def test_active_facility_without_records(detail_client):
    client, _, parking_id = detail_client
    response = client.get(f"/api/v1/parking/{parking_id}")
    assert response.status_code == 200
    assert response.json()["data"]["records"] == []


@pytest.mark.parametrize("inactive", [False, True])
def test_missing_or_inactive_facility(detail_client, inactive):
    client, session, parking_id = detail_client
    session.scalar.return_value = SimpleNamespace(is_active=False) if inactive else None
    response = client.get(f"/api/v1/parking/{parking_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PARKING_NOT_FOUND"
    session.scalars.assert_not_awaited()


def test_invalid_uuid(detail_client):
    client, session, _ = detail_client
    response = client.get("/api/v1/parking/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["data"] is None
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize("operation", ["scalar", "scalars"])
def test_database_failure_is_not_empty_success(detail_client, operation):
    client, session, parking_id = detail_client
    getattr(session, operation).side_effect = OperationalError(
        "secret-host", {}, Exception("secret")
    )
    response = client.get(f"/api/v1/parking/{parking_id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True
    assert "secret" not in response.text


def test_invalid_stored_detail_is_data_failure(detail_client):
    client, session, parking_id = detail_client
    session.scalars.return_value = [
        ParkingSourceRecord(source_record_id=uuid4(), source="test", registered_capacity=-1)
    ]
    response = client.get(f"/api/v1/parking/{parking_id}")
    assert response.status_code == 503
    assert "secret" not in response.text


def test_route_registered_once_and_openapi_schema(detail_client):
    client, _, _ = detail_client
    # FastAPI lazily includes child routers; check the generated public contract.
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        schema = client.get("/openapi.json").json()
    assert not any("Duplicate Operation ID" in str(w.message) for w in emitted)
    operation = schema["paths"]["/api/v1/parking/{parking_id}"]["get"]
    assert operation["tags"] == ["parking"]
    assert set(operation["responses"]) >= {"200", "404", "422", "503"}
    assert "ParkingDetailResponse" in schema["components"]["schemas"]
