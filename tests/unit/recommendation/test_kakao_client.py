import asyncio

import httpx
import pytest
from pydantic import SecretStr

from app.clients.errors import ProviderError
from app.clients.kakao import KakaoClient, parse_places, parse_route
from app.core.config import Settings
from app.schemas.recommendation import Conditions


@pytest.mark.parametrize(
    "url", ["https://place.map.kakao.com/123", None, "", "javascript:alert(1)"]
)
def test_place_url_is_preserved_or_null_without_losing_place(url):
    places = parse_places(
        {
            "documents": [
                {
                    "id": "123",
                    "place_name": "식당",
                    "x": "127",
                    "y": "37.5",
                    "place_url": url,
                }
            ]
        }
    )
    assert len(places) == 1
    result = places[0].model_dump(mode="json")
    assert result["place_url"] == (url if url and url.startswith("https://") else None)


def test_keyword_search_uses_distance_and_requested_page():
    captured = []

    def handler(request):
        captured.append(dict(request.url.params))
        return httpx.Response(200, json={"documents": [], "meta": {"is_end": True}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            kakao = KakaoClient(
                client, Settings(_env_file=None, kakao_rest_api_key=SecretStr("test"))
            )
            return await kakao.search(Conditions(query="파스타", x=126.978, y=37.5665), 2)

    assert asyncio.run(run()) == ([], True)
    assert captured == [
        {
            "query": "파스타",
            "x": "126.978",
            "y": "37.5665",
            "sort": "distance",
            "page": "2",
            "size": "15",
        }
    ]


@pytest.mark.parametrize(
    "payload", [{}, {"routes": []}, {"routes": [{}]}, {"routes": [{"summary": {"duration": 100}}]}]
)
def test_malformed_route_is_provider_error_not_no_route(payload):
    with pytest.raises(ProviderError) as error:
        parse_route(payload)
    assert error.value.status_code == 502
    assert error.value.code == "route_invalid"


def test_explicit_route_failure_remains_no_route():
    with pytest.raises(ProviderError) as error:
        parse_route({"routes": [{"result_code": 104, "result_msg": "no route"}]})
    assert error.value.code == "route_not_found"


def test_null_search_metadata_is_safe_provider_error():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"documents": [], "meta": None})
            )
        ) as client:
            kakao = KakaoClient(
                client, Settings(_env_file=None, kakao_rest_api_key=SecretStr("test"))
            )
            return await kakao.search(Conditions(query="파스타", x=127, y=37.5), 1)

    with pytest.raises(ProviderError) as error:
        asyncio.run(run())
    assert error.value.code == "kakao_invalid"
