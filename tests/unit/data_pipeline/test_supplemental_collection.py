import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.modules.data_pipeline.collectors import supplemental as collectors


def test_sdot_does_not_expand_beyond_ten_thousand_rows():
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "sDoTPeople": {
                    "RESULT": {"CODE": "INFO-000"},
                    "list_total_count": 12000,
                    "row": [{"SERIAL": "1", "SENSING_TIME": "2026-11-04_09:00:00"}] * 1000,
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_sdot(
                http,
                Settings(_env_file=None, seoul_sdot_api_key="test"),
                now=datetime(2026, 11, 4, 0, 30, tzinfo=UTC),
            )
            assert len(result["rows"]) == 10000 and result["provider_total"] is None
            assert result["query"]["windows"][0]["provider_total"] == 12000
            assert result["query_complete"] is False

    asyncio.run(run())
    assert len(calls) == 10 and calls[-1].endswith("/9001/10000/2026-11-04_09")


def test_national_parking_stops_at_five_pages_per_dataset_and_keeps_partial():
    calls = []

    def response(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        calls.append((method, int(request.url.params["pageNo"])))
        assert request.url.params["sidoCd"] == "11"
        return httpx.Response(
            200,
            json={
                "resultCode": "00",
                "totalCount": 6000,
                method: [
                    {"prk_center_id": f"{request.url.params['pageNo']}-{i}"} for i in range(1000)
                ],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_national_parking(http, Settings(_env_file=None))
            assert len(result["datasets"]) == 3
            assert all(
                len(data["rows"]) == 5000 and not data["complete"]
                for data in result["datasets"].values()
            )

    asyncio.run(run())
    assert len(calls) == 15 and max(page for _, page in calls) == 5


def test_sgis_auth_failure_does_not_continue_requests():
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"result": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            with pytest.raises(CollectionError, match="^sgis_auth_failed$"):
                await collectors.collect_areas(http, Settings(_env_file=None))

    asyncio.run(run())
    assert len(calls) == 1


def test_sgis_uses_current_host_and_preserves_missing_polygon_for_per_row_validation():
    calls = []
    rows = [{"cd": "one", "pg": "POLYGON EMPTY"}, {"cd": "two", "pg": None}]

    def response(request):
        assert request.url.host == "sgisapi.mods.go.kr"
        calls.append(request.url.path)
        if request.url.path.endswith("authentication.json"):
            body = {"result": {"accessToken": "test"}}
        elif request.url.path.endswith("stage.json"):
            body = {
                "errCd": 0,
                "result": [{"cd": "11010"}] if request.url.params["cd"] == "11" else rows,
            }
        else:
            body = {"errCd": 0, "features": []}
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_areas(http, Settings(_env_file=None))
            assert [row for _, row in result["areas"]] == rows

    asyncio.run(run())
    assert len(calls) == 4


def test_sgis_failed_district_keeps_successful_rows_and_continues_boundary():
    def response(request):
        if request.url.path.endswith("authentication.json"):
            return httpx.Response(200, json={"result": {"accessToken": "test"}})
        if request.url.path.endswith("stage.json"):
            code = request.url.params["cd"]
            if code == "11":
                rows = [{"cd": "11010"}, {"cd": "11020"}]
            elif code == "11010":
                return httpx.Response(503)
            else:
                rows = [{"cd": "11020510", "pg": "current geometry"}]
            return httpx.Response(200, json={"errCd": 0, "result": rows})
        return httpx.Response(200, json={"errCd": 0, "features": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_areas(http, Settings(_env_file=None))
            assert result["complete"] is False
            assert [row["cd"] for _, row in result["areas"]] == ["11020510"]
            assert result["issues"] == [
                {
                    "district": "11010",
                    "error_code": "sgis_district_collection_failed",
                }
            ]

    asyncio.run(run())
