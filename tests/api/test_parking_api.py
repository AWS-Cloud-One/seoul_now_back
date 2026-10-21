from unittest.mock import AsyncMock

import pytest

from app.api.dependencies import get_session


@pytest.mark.parametrize(
    "query", ["", "lat=0&lon=0", "lat=nan&lon=127", "lat=37.5&lon=127&limit=1001"]
)
def test_nearby_rejects_invalid_requests(app, client, query):
    session = AsyncMock()
    app.dependency_overrides[get_session] = lambda: session
    response = client.get(f"/api/v1/parking/nearby?{query}")
    assert response.status_code == 422
    session.scalar.assert_not_awaited()


def test_no_data_is_not_claimed_to_mean_no_parking(app, client):
    session = AsyncMock()
    session.scalar.return_value = None
    app.dependency_overrides[get_session] = lambda: session
    response = client.get("/api/v1/parking/nearby?lat=37.56&lon=126.97")
    assert response.status_code == 200
    assert response.json()["status"] == "unconfirmed"
    assert response.json()["items"] == []


def test_inspection_page_does_not_trigger_collection(client):
    response = client.get("/api/v1/parking/inspect")
    assert response.status_code == 200
    assert "서울 공영주차장 등록정보" in response.text
    assert "서울 실시간 도시데이터" in response.text
    assert "가상 시연 데이터" not in response.text
