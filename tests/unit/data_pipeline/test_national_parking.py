import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.modules.data_pipeline.collectors.national_parking import METHODS, collect_national_parking
from app.modules.data_pipeline.normalizers import national_parking as normalizer


@pytest.mark.parametrize("failed", METHODS)
def test_one_method_failure_does_not_discard_other_responses(failed):
    calls = []

    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        calls.append(method)
        return (
            httpx.Response(503)
            if method == failed
            else httpx.Response(
                200, json={"resultCode": "0", "totalCount": "1", method: [{"prk_center_id": "a"}]}
            )
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await collect_national_parking(http, Settings(_env_file=None))
        assert calls == list(METHODS)
        for method, data in result["datasets"].items():
            assert data["status"] == ("failed" if method == failed else "success")
            assert data["complete"] == (method != failed)
            assert data["pages"][0]["received_at"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode,error",
    [
        ("short", "parking_premature_end"),
        ("empty", "parking_premature_end"),
        ("repeat", "parking_repeated_page"),
        ("total", "parking_total_changed"),
        ("echo", "parking_page_mismatch"),
        ("bad_count", "parking_invalid_page"),
        ("xml", "parking_invalid_response"),
    ],
)
def test_incomplete_or_invalid_pages_never_claim_complete(mode, error):
    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        page = int(request.url.params["pageNo"])
        if mode == "xml":
            return httpx.Response(200, text="<error>auth</error>")
        rows = [{"prk_center_id": "a"}, {"prk_center_id": "b"}]
        data = {"resultCode": "00", "totalCount": 4, method: rows, "pageNo": page}
        if mode in {"short", "empty"}:
            data[method] = rows[:1] if mode == "short" else []
        elif mode == "total" and page == 2:
            data["totalCount"] = 5
        elif mode == "echo":
            data["pageNo"] = 99
        elif mode == "bad_count":
            data["totalCount"] = "2.5"
        return httpx.Response(200, json=data)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            data = await collect_national_parking(http, Settings(_env_file=None), page_size=2)
        assert all(
            not d["complete"] and d["error_code"] == error for d in data["datasets"].values()
        )
        if mode in {"repeat", "total"}:
            assert all(
                len(d["rows"]) == 2 and len(d["pages"]) == 2 for d in data["datasets"].values()
            )

    asyncio.run(run())


def test_empty_success_and_per_method_zero_are_not_provider_failures():
    async def run():
        def respond(request):
            method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
            return httpx.Response(200, json={"resultCode": "0", "totalCount": 0, method: []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            data = await collect_national_parking(http, Settings(_env_file=None))
        assert all(d["complete"] and d["status"] == "success" for d in data["datasets"].values())

    asyncio.run(run())


@pytest.mark.parametrize(
    "start,end,state,offset",
    [
        ("091530", "180015", "valid", 0),
        ("000000", "240000", "valid", 1),
        ("000000", "000000", "unknown", None),
        ("220000", "060000", "unknown", None),
        ("256100", "180000", "invalid", None),
        ("0900", "180000", "invalid", None),
        (90000, "180000", "invalid", None),
    ],
)
def test_hhmmss_does_not_truncate_seconds_or_guess_overnight(start, end, state, offset):
    period = normalizer.hours("Monday", {"opertn_start_time": start, "opertn_end_time": end})[0]
    assert (period["day_type"], period["state"], period["end_day_offset"]) == (
        "monday",
        state,
        offset,
    )
    if start == "091530":
        assert period["opens_at"] == "09:15:30" and period["closes_at"] == "18:00:15"


def test_multiple_operating_periods_keep_sequence_and_hold_overlap():
    rows = [
        {"opertn_start_time": "090000", "opertn_end_time": "120000"},
        {"opertn_start_time": "130000", "opertn_end_time": "180000"},
    ]
    values = normalizer.hours("Monday", rows)
    assert [p["period_no"] for p in values] == [1, 2]
    assert all(p["state"] == "valid" for p in values)
    rows[1]["opertn_start_time"] = "100000"
    assert all(p["reason_code"] == "OVERLAPPING_HOURS" for p in normalizer.hours("Monday", rows))


def test_fee_intervals_use_minutes_without_treating_daily_price_as_a_cap():
    value = normalizer.operation(
        {
            "basic_info": {
                "parking_chrge_bs_time": "30",
                "parking_chrge_bs_chrge": "1200",
                "parking_chrge_adit_unit_time": "10",
                "parking_chrge_adit_unit_chrge": "0",
            },
            "fxamt_info": {
                "parking_chrge_one_day_chrge": "10000",
                "parking_chrge_mon_unit_chrge": "100000",
            },
        }
    )
    fees = value["fees"]
    assert fees["base_minutes"] == 30 and fees["additional_minutes"] == 10
    assert fees["base_fee_krw"] == 1200 and fees["daily_max_krw"] is None
    assert fees["additional_fee_krw"] == 0 and fees["daily_flat_fee_krw"] == 10000
    assert fees["base_usable"] and fees["additional_usable"]
    assert "monthly_flat_fee_krw" not in fees  # Monthly passes were explicitly excluded.
    assert "UNCONFIRMED_TIME_UNIT" not in value["field_issues"].values()


@pytest.mark.parametrize(
    "minutes,fee,usable,reason",
    [
        ("30", "0", True, None),
        ("0", "1200", False, "NONPOSITIVE_BILLING_INTERVAL"),
        ("", "1200", False, "INCOMPLETE_FEE_PAIR"),
        ("-1", "1200", False, "INCOMPLETE_FEE_PAIR"),
        ("30", "", False, "INCOMPLETE_FEE_PAIR"),
    ],
)
def test_billing_pair_validates_interval_and_accepts_zero_fee(minutes, fee, usable, reason):
    value = normalizer.operation(
        {"basic_info": {"parking_chrge_bs_time": minutes, "parking_chrge_bs_chrge": fee}}
    )
    assert value["fees"]["base_usable"] is usable
    assert value["field_issues"].get("fees.base") == reason


@pytest.mark.parametrize(
    "available,reason",
    [
        ("0", "SOURCE_TIME_UNKNOWN"),
        ("", "INVALID_OR_MISSING_COUNT"),
        ("11", "COUNT_EXCEEDS_CAPACITY"),
    ],
)
def test_missing_observation_time_never_becomes_receipt_time(available, reason):
    live = normalizer.availability(
        {"pkfc_ParkingLots_total": "10", "pkfc_Available_ParkingLots_total": available},
        datetime.now(UTC),
    )
    assert not live["usable"] and live["observed_at"] is None and live["reason_code"] == reason
    if available == "0":
        assert live["reported_available_spaces"] == 0


def test_duplicates_and_conflicting_ids_are_order_independent():
    def candidate(name):
        return SimpleNamespace(
            record={"prk_center_id": "a", "name": name},
            received_at=datetime(2020, 1, 1, tzinfo=UTC),
            origin=name,
            row_index=0,
        )

    rows = [candidate("one"), candidate("one"), candidate("two")]
    for ordered in (rows, list(reversed(rows))):
        selected, duplicates, reasons, blocked = normalizer.select_records(ordered)
        assert not selected and duplicates == 1
        assert reasons == {"conflicting_management_id": 2} and blocked == {"a"}


def test_selection_discards_unused_fields_and_preserves_operational_values():
    row = {
        "prk_center_id": "a",
        "unused_provider_value": "discard",
        "basic_info": {
            "parking_chrge_bs_time": "30",
            "parking_chrge_bs_chrge": "1200",
            "parking_chrge_adit_unit_time": "10",
            "parking_chrge_adit_unit_chrge": "0",
            "unused": "discard",
        },
        "fxamt_info": {"parking_chrge_one_day_chrge": "10000", "monthly": "discard"},
        "Monday": [
            {"opertn_start_time": "090000", "opertn_end_time": "180000", "unused": "discard"}
        ],
    }
    selected = normalizer.selected_fields("PrkOprInfo", row)
    assert normalizer.operation(selected) == normalizer.operation(row)
    assert "unused_provider_value" not in selected
    assert "unused" not in selected["basic_info"] and "unused" not in selected["Monday"][0]
    assert "monthly" not in selected["fxamt_info"]


def test_collector_returns_selected_fields_and_short_errors_without_response_bodies():
    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        if method == METHODS[1]:
            return httpx.Response(200, text="<private_response>discard</private_response>")
        return httpx.Response(
            200,
            json={
                "resultCode": "0",
                "totalCount": 1,
                method: [{"prk_center_id": "a", "unneeded": "discard"}],
                "other_section": "discard",
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            data = await collect_national_parking(http, Settings(_env_file=None))
        for method, dataset in data["datasets"].items():
            assert all("body" not in page for page in dataset["pages"])
            if method == METHODS[1]:
                assert dataset["error_code"] == "parking_invalid_response"
                assert not dataset["rows"]
            else:
                assert dataset["rows"] == [{"prk_center_id": "a"}]

    asyncio.run(run())
