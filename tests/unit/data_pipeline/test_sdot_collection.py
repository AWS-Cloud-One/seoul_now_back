import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.seoul import CollectionError, SeoulClient
from app.core.config import Settings
from app.modules.data_pipeline.collectors.supplemental import collect_sdot, read_sensor_file
from app.modules.data_pipeline.normalizers.sdot import sensor_locations
from tests.fixtures.sdot import observation


def collect(responses, *, now=datetime(2026, 11, 4, 0, 30, tzinfo=UTC)):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        body = (
            responses[len(calls) - 1]
            if len(calls) <= len(responses)
            else {"RESULT": {"CODE": "INFO-200"}}
        )
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await collect_sdot(
                http, Settings(_env_file=None, seoul_sdot_api_key="test"), now=now
            )

    return asyncio.run(run()), calls


def page(total, rows):
    rows = [{**r, "SENSING_TIME": r["SENSING_TIME"].replace(" ", "_", 1)} for r in rows]
    return {"sDoTPeople": {"RESULT": {"CODE": "INFO-000"}, "list_total_count": total, "row": rows}}


def test_early_empty_page_keeps_original_total_and_rows():
    rows = [observation(str(i + 1)) for i in range(1000)]
    result, calls = collect([page(2000, rows), {"RESULT": {"CODE": "INFO-200"}}])
    assert len(calls) == 4 and len(result["rows"]) == 1000
    assert result["provider_total"] == 2000
    assert [p["provider_total"] for p in result["pages"][:2]] == [2000, 0]
    assert "unexpected_empty_page" in result["issues"]
    assert not result["query_complete"]
    assert result["row_contexts"][0]["received_at"]


def test_total_change_duplicate_rows_and_short_page_are_recorded():
    rows = [observation(str(i + 1)) for i in range(1000)]
    result, _ = collect([page(2000, rows), page(2001, rows[:1])])
    assert result["provider_total"] == 2000
    assert result["issues"] == [
        "duplicate_page_rows",
        "provider_total_changed",
        "unexpected_page_size",
    ]
    assert result["pages"][1]["duplicate_rows"] == 1
    assert not result["query_complete"]


def test_later_transport_failure_keeps_earlier_page_evidence():
    result, calls = collect([page(2000, [observation(str(i + 1)) for i in range(1000)]), 403])
    assert len(calls) == 4 and len(result["rows"]) == 1000
    assert result["pages"][1]["error_code"] == "provider_auth_error"
    assert not result["query_complete"]


def test_first_empty_page_and_finite_range_do_not_claim_latest_all_sensors():
    empty, _ = collect([{"RESULT": {"CODE": "INFO-200"}}])
    assert empty["provider_total"] == 0 and empty["query_complete"]
    result, _ = collect([page(1, [observation()])])
    assert result["query_complete"]
    assert result["query"]["sensing_time_prefixes"] == [
        "2026-11-04_09",
        "2026-11-04_08",
        "2026-11-04_07",
    ]
    assert result["query"]["ordering"] == "client_sensing_time_then_provider_date"
    assert not result["query"]["snapshot_consistent"]


def test_midnight_queries_prior_day_and_checks_returned_window():
    result, calls = collect([], now=datetime(2026, 11, 3, 15, 2, tzinfo=UTC))
    assert [p.rsplit("/", 1)[-1] for p in calls] == [
        "2026-11-04_00",
        "2026-11-03_23",
        "2026-11-03_22",
    ]
    assert result["query_complete"]
    result, _ = collect([page(1, [observation(SENSING_TIME="2026-11-04 08:00")])])
    assert result["rows"] == []
    assert "sensing_time_filter_not_respected" in result["issues"]
    assert not result["query_complete"]
    assert result["pages"][0]["body"]["sDoTPeople"]["row"]


def test_page_budget_is_shared_between_hour_windows():
    responses = [
        page(20000, [observation(str(page_no * 1000 + i + 1)) for i in range(1000)])
        for page_no in range(10)
    ]
    result, calls = collect(responses)
    assert len(calls) == 10 and len(result["rows"]) == 10000
    assert "query_page_limit" in result["issues"] and not result["query_complete"]


def test_oversized_provider_page_is_archived_but_not_accepted_beyond_budget():
    result, _ = collect([page(10001, [observation(str(i)) for i in range(10001)])])
    assert not result["rows"] and not result["query_complete"]
    assert "unexpected_page_size" in result["issues"]
    assert len(result["pages"][0]["body"]["sDoTPeople"]["row"]) == 10001


@pytest.mark.parametrize("total", [True, 1.5, "1.5", -1, None])
def test_page_total_does_not_silently_truncate(total):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page(total, [])))
        ) as http:
            with pytest.raises(CollectionError, match="provider_invalid_response"):
                await SeoulClient(http, "test", "sDoTPeople").fetch_page(1, 1000)

    asyncio.run(run())


def test_real_installation_file_has_hash_pinned_date_and_device_serials():
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "data/reference/sdot_locations_20251113.xlsx"
    file = read_sensor_file(path)
    reference = sensor_locations(file)
    assert file["reference_date"] == "2025-11-13"
    assert file["received_at"] is None
    assert len(reference["locations"]) == 134
    assert all(i["device_serial"] for i in reference["locations"].values())
