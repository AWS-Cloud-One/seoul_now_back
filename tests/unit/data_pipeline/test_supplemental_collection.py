import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.normalizers import supplemental as normalizers


def test_sdot_keeps_zero_unobserved_locations_and_conflicting_counts():
    locations = {code: {"id": code, "lat": 37.5, "lng": 127} for code in ("3011", "2", "3")}
    rows = [
        {"SERIAL": "00000003011", "SENSING_TIME": "2026-10-25_18:00:00", "VISITOR_COUNT": str(n)}
        for n in (1, 2)
    ]
    rows.append({"SERIAL": "2", "SENSING_TIME": "2026-10-25_18:00:00", "VISITOR_COUNT": "0"})
    items = {row["id"]: row for row in normalizers.latest_sensors(locations, rows)}
    assert items["3011"]["observation_status"] == "conflicting"
    assert items["3011"]["count_10min"] is None
    assert items["2"]["count_10min"] == 0
    assert items["2"]["observed_at"].endswith("+09:00")
    assert items["3"]["count_10min"] is None and items["3"]["observed_at"] is None
    assert all("observed_at" not in value for value in locations.values())


def test_official_sensor_file_and_location_validation():
    path = Path(__file__).resolve().parents[3] / "data/reference/sdot_locations_20251113.xlsx"
    locations = normalizers.sensor_locations(collectors.read_sensor_rows(path))
    assert locations and all(item["address"].startswith("서울") for item in locations.values())
    assert normalizers.sensor_locations(
        [
            {"B": "0001", "D": "서울특별시", "E": "37.5", "F": "127"},
            {"B": "2", "D": "서울특별시", "E": "NaN", "F": "127"},
            {"B": "3", "D": "부산광역시", "E": "37.5", "F": "127"},
        ]
    ) == {"1": {"id": "1", "address": "서울특별시", "lat": 37.5, "lng": 127.0}}


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
                    "row": [{"SERIAL": "1"}] * 1000,
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            result = await collectors.collect_sdot(
                http, Settings(_env_file=None, seoul_sdot_api_key="test")
            )
            assert len(result["rows"]) == 10000 and result["provider_total"] == 12000
            assert normalizers.sdot_snapshot({}, result)["complete_history"] is False

    asyncio.run(run())
    assert len(calls) == 10 and calls[-1].endswith("/9001/10000/")


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
                method: [{"prk_center_id": "1"}] * 1000,
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
