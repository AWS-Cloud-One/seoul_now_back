"""Seoul application error codes share bounded transport retry and stop rules."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.seoul import CollectionError, SeoulClient
from app.modules.data_pipeline.collectors.citydata import collect_citydata
from app.modules.data_pipeline.collectors.topis import collect_topis


@pytest.mark.parametrize("provider", ["parking", "citydata"])
@pytest.mark.parametrize("code", ["ERROR-500", "ERROR-600"])
def test_body_server_error_can_recover_on_next_attempt(monkeypatch, provider, code):
    calls = []
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"RESULT": {"CODE": code}})
        body = (
            {"RESULT": {"CODE": "INFO-000"}, "CITYDATA": {"AREA_CD": "POI001", "AREA_NM": "test"}}
            if provider == "citydata"
            else {
                "GetParkInfo": {
                    "RESULT": {"CODE": "INFO-000"},
                    "list_total_count": 1,
                    "row": [{"PKLT_CD": "1"}],
                }
            }
        )
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            if provider == "citydata":
                document = await collect_citydata(http, "secret", "test", expected_code="POI001")
                assert document.body["CITYDATA"]["AREA_CD"] == "POI001"
            else:
                assert await SeoulClient(http, "secret").fetch_page(1, 1000) == (
                    1,
                    [{"PKLT_CD": "1"}],
                )

    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.parametrize("provider", ["parking", "citydata", "topis"])
@pytest.mark.parametrize(
    "code,error",
    [
        ("INFO-100", "provider_auth_error"),
        ("ERROR-337", "provider_quota_exceeded"),
        ("ERROR-200", "provider_result_error"),
    ],
)
def test_nontransient_body_errors_are_not_retried_or_leaked(provider, code, error):
    calls = []

    def respond(request):
        calls.append(request)
        if provider == "topis":
            return httpx.Response(200, text=f"<RESULT><CODE>{code}</CODE></RESULT>")
        return httpx.Response(200, json={"RESULT": {"CODE": code, "MESSAGE": "secret"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises(CollectionError, match=f"^{error}$"):
                if provider == "citydata":
                    await collect_citydata(http, "secret", "test", expected_code="POI001")
                elif provider == "topis":
                    await collect_topis(http, "secret", "1000000000")
                else:
                    await SeoulClient(http, "secret").fetch_page(1, 1000)

    asyncio.run(run())
    assert len(calls) == 1
