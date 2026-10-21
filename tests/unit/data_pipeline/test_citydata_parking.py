import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors.citydata_parking import collect_citydata_parking
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    normalize_parking,
)
from app.repositories.parking import parking_availability


def test_fetches_nested_parking_status_without_logging_credentials():
    calls = []
    fixture = {
        "RESULT": {"CODE": "INFO-000"},
        "CITYDATA": {
            "AREA_NM": "광화문·덕수궁",
            "PRK_STTS": [
                {
                    "PRK_NM": "샘플 주차장",
                    "PRK_CD": "3202173",
                    "CPCTY": "24",
                    "CUR_PRK_CNT": "7",
                    "CUR_PRK_TIME": "2026-10-22 12:00:00",
                    "CUR_PRK_YN": "Y",
                    "PAY_YN": "Y",
                    "RATES": "2000",
                    "TIME_RATES": "30",
                    "ADD_RATES": "1000",
                    "ADD_TIME_RATES": "10",
                    "ADDRESS": "중구 북창동 18-9",
                    "ROAD_ADDR": "",
                    "LNG": "126.977556",
                    "LAT": "37.562981",
                }
            ],
        },
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            rows = await collect_citydata_parking(http, "test-key", "광화문·덕수궁")
            assert len(rows) == 1
            assert rows[0]["CUR_PRK_YN"] == "Y"

    asyncio.run(check())
    assert calls[0].url.path.endswith("/json/citydata/1/5/광화문·덕수궁")
    assert "test-key" in calls[0].url.path


def test_rejects_provider_error_without_leaking_message():
    def handler(request):
        return httpx.Response(200, json={"RESULT": {"CODE": "ERROR-300", "MESSAGE": "private"}})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(CollectionError, match="^provider_result_error$"):
                await collect_citydata_parking(http, "secret", "광화문·덕수궁")

    asyncio.run(check())


def test_normalizes_current_parking_fields_and_marks_source_time():
    row = {
        "PRK_NM": "샘플 주차장",
        "PRK_CD": "3202173",
        "CPCTY": "24",
        "CUR_PRK_CNT": "7",
        "CUR_PRK_TIME": "2026-10-22 12:00:00",
        "CUR_PRK_YN": "Y",
        "PAY_YN": "Y",
        "RATES": "2000",
        "TIME_RATES": "30",
        "ADD_RATES": "1000",
        "ADD_TIME_RATES": "10",
        "ADDRESS": "중구 북창동 18-9",
        "ROAD_ADDR": "",
        "LNG": "126.977556",
        "LAT": "37.562981",
    }
    normalized = normalize_parking(row, datetime.now(UTC), CITYDATA_PARKING_SOURCE)
    assert normalized["source_id"] == "3202173"
    assert normalized["details"]["CUR_PRK_CNT"] == "7"
    assert normalized["details"]["CUR_PRK_YN"] == "Y"
    assert normalized["source_updated_at"].isoformat().startswith("2026-10-22T12:00:00")


def test_citydata_availability_requires_provider_flag_and_fresh_timestamp():
    now = datetime(2026, 10, 22, 0, 35, tzinfo=UTC)
    fresh = now - timedelta(minutes=5)
    live = parking_availability(
        CITYDATA_PARKING_SOURCE, {"CUR_PRK_YN": "Y", "CUR_PRK_CNT": "162"}, fresh, now
    )
    assert live["real_time_available"] is True
    assert live["available_spaces"] == 162

    unavailable = parking_availability(
        CITYDATA_PARKING_SOURCE, {"CUR_PRK_YN": "N", "CUR_PRK_CNT": "0"}, fresh, now
    )
    assert unavailable["availability_status"] == "not_provided"
    assert unavailable["available_spaces"] is None

    stale = parking_availability(
        CITYDATA_PARKING_SOURCE,
        {"CUR_PRK_YN": "Y", "CUR_PRK_CNT": "162"},
        now - timedelta(minutes=16),
        now,
    )
    assert stale["availability_status"] == "stale"
    assert stale["available_spaces"] is None

    malformed = parking_availability(
        CITYDATA_PARKING_SOURCE, {"CUR_PRK_YN": "Y", "CUR_PRK_CNT": "?"}, fresh, now
    )
    assert malformed["availability_status"] == "unconfirmed"
    assert malformed["available_spaces"] is None
