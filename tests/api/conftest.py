from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_recommendation_geography
from app.core.config import Settings
from app.main import create_app
from app.services.recommendation_geography import RoadGrade


@pytest.fixture
def app() -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://test:test@127.0.0.1:1/test",
            cors_origins=["http://localhost:5173"],
            docs_enabled=True,
            health_timeout_seconds=0.05,
            # These API tests have no database; keep in-flight state in this process.
            state_backend="memory",
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def geography(app):
    reader = AsyncMock()
    reader.areas.return_value = []
    reader.traffic.return_value = RoadGrade()
    app.dependency_overrides[get_recommendation_geography] = lambda: reader
    return reader
