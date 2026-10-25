from unittest.mock import AsyncMock, patch

import httpx


def test_places_requires_key(app, client):
    app.state.settings.kakao_rest_api_key = __import__("pydantic").SecretStr("")
    assert client.get("/api/v1/kakao/places?query=카페").status_code == 503


def test_places_validates_coordinates(client):
    assert client.get("/api/v1/kakao/places?query=cafe&lat=99").status_code == 422


def test_directions_flattens_geometry(client):
    payload = {
        "routes": [
            {
                "result_code": 0,
                "summary": {"distance": 100},
                "sections": [{"roads": [{"vertexes": [127, 37, 127.1, 37.1]}]}],
            }
        ]
    }
    with patch("app.api.routes.kakao.request_provider", new=AsyncMock(return_value=payload)):
        response = client.get(
            "/api/v1/kakao/directions",
            params={
                "origin_lat": 37.5,
                "origin_lon": 127,
                "destination_lat": 37.6,
                "destination_lon": 127.1,
            },
        )
    assert response.status_code == 200
    assert response.json()["coordinates"] == [[127, 37], [127.1, 37.1]]


def test_provider_error_hides_credentials(app, client):
    from pydantic import SecretStr

    app.state.settings.kakao_rest_api_key = SecretStr("secret-test-key")
    with patch(
        "httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("secret-test-key"))
    ):
        response = client.get("/api/v1/kakao/places?query=cafe")
    assert response.status_code == 502
    assert "secret-test-key" not in response.text
