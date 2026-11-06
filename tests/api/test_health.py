import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_session


def test_liveness_does_not_need_database(app: FastAPI, client: TestClient) -> None:
    def fail_if_session_requested() -> None:
        raise AssertionError("Liveness must not request a database session")

    app.dependency_overrides[get_session] = fail_if_session_requested
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_with_available_database(app: FastAPI, client: TestClient) -> None:
    session = AsyncMock()
    app.dependency_overrides[get_session] = lambda: session
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    session.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT PostGIS_Version()", {}, Exception("secret=do-not-leak")),
        OSError("secret=do-not-leak"),
    ],
)
def test_readiness_hides_database_errors(
    app: FastAPI, client: TestClient, error: Exception
) -> None:
    session = AsyncMock()
    session.execute.side_effect = error
    app.dependency_overrides[get_session] = lambda: session
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database or PostGIS is unavailable"}


def test_readiness_times_out(app: FastAPI, client: TestClient) -> None:
    async def slow_query(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(1)

    session = AsyncMock()
    session.execute.side_effect = slow_query
    app.dependency_overrides[get_session] = lambda: session
    assert client.get("/api/v1/health/ready").status_code == 503


def test_cors_allows_frontend_only(client: TestClient) -> None:
    for origin in ["http://localhost:5173", "https://untrusted.example"]:
        response = client.options(
            "/api/v1/health/live",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        if origin == "http://localhost:5173":
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
        else:
            assert response.status_code == 400
            assert "access-control-allow-origin" not in response.headers


def test_openapi_lists_health_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/health/live" in response.json()["paths"]
    assert "/api/v1/health/ready" in response.json()["paths"]
