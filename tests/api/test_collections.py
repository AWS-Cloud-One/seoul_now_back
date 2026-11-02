from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.api.dependencies import get_session


def test_collection_runs_filter_and_limit(client, app):
    now = datetime.now(UTC)
    run = SimpleNamespace(
        id=uuid4(),
        source="seoul_citydata",
        area_code="POI001",
        area_name="test",
        started_at=now,
        finished_at=now,
        status="failed",
        received=0,
        accepted=0,
        rejected=0,
        duplicates=0,
        error_code="provider_network_error",
        rejection_reasons={},
    )

    class Session:
        async def scalars(self, statement):
            params = statement.compile().params
            assert "seoul_citydata" in params.values()
            assert 1 in params.values()
            return [run]

    async def session():
        yield Session()

    app.dependency_overrides[get_session] = session
    response = client.get("/api/v1/collections/runs?source=seoul_citydata&limit=1")
    assert response.status_code == 200
    assert response.json()["items"][0]["error_code"] == "provider_network_error"
    assert client.get("/api/v1/collections/runs?limit=201").status_code == 422
