import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.normalizers import supplemental as normalizers


def test_cctv_conversion_preserves_filtered_order_and_does_not_invent_observations():
    rows = [
        {
            "roadsectionid": "a",
            "coordx": "127",
            "coordy": "37.5",
            "cctvurl": "https://example.com/a",
        },
        {"roadsectionid": "bad", "coordx": "NaN", "coordy": "37.5"},
        {"roadsectionid": "b", "coordx": "127.1", "coordy": "37.6"},
    ]
    candidates = normalizers.cctv_candidates(rows)
    snapshot = normalizers.cctv_snapshot(
        candidates[1:], {"rows": rows, "fetched_at": datetime(2026, 10, 25, tzinfo=UTC)}
    )
    assert snapshot["received"] == 3 and snapshot["items"][0]["id"] == "b:0"
    assert snapshot["items"][0]["direction"] is None
    assert snapshot["items"][0]["playback_status"] == "unverified"
    assert candidates[1]["roadsectionid"] == "b"


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


def test_cctv_probe_reuses_file_time_without_an_http_call(tmp_path):
    path = tmp_path / "probe.json"
    rows = [{"roadsectionid": "test", "coordx": 127, "coordy": 37.5}]
    path.write_text(json.dumps({"response": {"data": rows}}))

    def response(request):
        pytest.fail("a reused probe must not spend ITS quota")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_cctv(http, Settings(_env_file=None), probe_path=path)
            assert result["rows"] == rows
            assert result["fetched_at"] == datetime.fromtimestamp(path.stat().st_mtime, UTC)

    asyncio.run(run())
