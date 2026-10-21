import asyncio

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors.citydata import collect_citydata


def test_collect_citydata_preserves_all_sections_and_encodes_area():
    payload = {
        "AREA_NM": "서울역",
        "AREA_CD": "POI001",
        "LIVE_PPLTN_STTS": [{"AREA_PPLTN_MIN": "1000"}],
        "PRK_STTS": [{"PRK_NM": "주차장"}],
        "FUTURE_SECTION": {"x": 1},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/서울역")
        return httpx.Response(200, json={"CITYDATA": payload})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_citydata(client, "test-key", "서울역")

    result = asyncio.run(run())
    assert result == payload
    assert result["FUTURE_SECTION"] == {"x": 1}


def test_collect_citydata_rejects_provider_error_without_leaking_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"RESULT": {"CODE": "ERROR-200"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CollectionError, match="provider_result_error"):
                await collect_citydata(client, "secret-key", "서울역")

    asyncio.run(run())


def test_collect_citydata_requires_complete_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"CITYDATA": {"AREA_NM": "서울역"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CollectionError, match="provider_invalid_response"):
                await collect_citydata(client, "secret-key", "서울역")

    asyncio.run(run())
