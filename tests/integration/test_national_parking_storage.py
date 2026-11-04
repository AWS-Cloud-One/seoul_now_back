"""National method joins, partial collections and independent evidence on PostGIS."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import (
    CollectionRun,
    ParkingAvailabilityLatest,
    ParkingFieldIssue,
    ParkingSourceRecord,
    RawPayload,
)
from app.modules.data_pipeline.normalizers.national_parking import METHODS
from app.modules.data_pipeline.pipelines.national_parking import save_national_parking
from app.modules.data_pipeline.pipelines.supplemental import run_supplemental
from app.repositories.national_parking import NationalParkingWriter
from app.repositories.parking import ParkingRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.services.parking_registration import registered_information
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration


def facility(code="a", **changes):
    return {
        "prk_center_id": code,
        "prk_plce_nm": code,
        "prk_plce_adres": "서울 주소",
        "prk_plce_adres_sido": "서울특별시",
        "prk_plce_entrc_la": "37.56",
        "prk_plce_entrc_lo": "126.97",
        "prk_cmprt_co": "10",
        "parkng_chrge_se": "2",
        "parkng_chrge_se_nm": "유료",
        "prk_manage_agent": "1",
        "prk_manage_agent_nm": "공영",
        **changes,
    }


def operation(code="a", **changes):
    return {
        "prk_center_id": code,
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
        "Monday": [
            {"opertn_start_time": "091530", "opertn_end_time": "120000"},
            {"opertn_start_time": "130000", "opertn_end_time": "180000"},
        ],
        **changes,
    }


def live(code="a", count="0"):
    return {
        "prk_center_id": code,
        "pkfc_ParkingLots_total": "10",
        "pkfc_Available_ParkingLots_total": count,
    }


async def collect(engine, rows, failed=()):
    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        if method in failed:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "resultCode": "0",
                "totalCount": len(rows[method]),
                "pageNo": 1,
                method: rows[method],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        return await run_supplemental(engine, http, Settings(_env_file=None), "parking")


def test_methods_join_by_id_with_separate_provenance_and_unknown_live_time(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = dict(
            zip(
                METHODS,
                ([facility(), facility("b")], [operation(), operation("unmatched")], [live()]),
                strict=True,
            )
        )
        try:
            result = await collect(engine, rows)
            assert (
                result["status"] == "partial"
                and result["accepted"] == 4
                and result["rejected"] == 1
            )
            assert result["datasets"]["PrkOprInfo"]["rejection_reasons"] == {
                "unmatched_management_id": 1
            }
            assert result["datasets"]["PrkRealtimeInfo"]["status"] == "success"
            assert result["datasets"]["PrkRealtimeInfo"]["limitations"] == {}
            async with factory() as session:
                records = {
                    r.provider_record_id: r
                    for r in await session.scalars(select(ParkingSourceRecord))
                }
                a, b = records["a"], records["b"]
                assert a.is_paid and a.management_type_name == "공영"
                assert a.base_fee_krw == 1200 and a.additional_fee_krw == 0
                assert a.base_minutes == 30 and a.base_usable
                assert a.additional_minutes == 10 and a.additional_usable
                assert a.daily_max_krw is None and a.daily_flat_fee_krw == 10000
                assert b.operation_payload_id is None
                assert a.last_payload_id != a.operation_payload_id
                detail = (await ParkingDetailRepository(session).list_records(a.parking_id))[0]
                assert registered_information(detail)["collected_at"] is None
                assert (
                    "09:15:30"
                    in registered_information(detail)["value"]["operating_hours"]["notes"]
                )
                monday = [h for h in detail.operating_hours if h.day_type == "monday"]
                assert len(monday) == 2 and monday[0].opens_at.second == 30
                assert all(h.payload_id == a.operation_payload_id for h in monday)
                state = await session.get(ParkingAvailabilityLatest, a.source_record_id)
                assert (
                    state.attempt_reported_available_spaces == 0
                    and state.attempt_total_spaces == 10
                )
                assert (
                    state.observed_at is state.reported_available_spaces is state.payload_id is None
                )
                assert not state.usable and state.attempt_status == "unknown"
                assert state.attempt_reason_code == "SOURCE_TIME_UNKNOWN"
                assert state.attempt_payload_id not in (a.last_payload_id, a.operation_payload_id)
                reader = ParkingRepository(session)
                receipt = state.attempt_received_at
                reading = await reader.availability(a.source_record_id, now=receipt)
                assert reading.usable and reading.available_spaces == 0
                assert reading.observed_at is None and reading.time_basis == "received_at"
                assert reading.expires_at == receipt + timedelta(hours=1)
                assert reading.reason_code == "SOURCE_TIME_UNKNOWN"
                assert (
                    await reader.availability(a.source_record_id, now=reading.expires_at)
                ).usable
                expired = await reader.availability(
                    a.source_record_id, now=reading.expires_at + timedelta(seconds=1)
                )
                assert not expired.usable and expired.available_spaces is None
                assert expired.reason_code == "STALE_DATA"
                future = await reader.availability(
                    a.source_record_id, now=receipt - timedelta(seconds=1)
                )
                assert not future.usable and future.reason_code == "FUTURE_TIMESTAMP"
                state.reason_code = "AMBIGUOUS_AVAILABILITY_SCOPE"
                await session.flush()
                ambiguous = await reader.availability(a.source_record_id, now=receipt)
                assert not ambiguous.usable and ambiguous.available_spaces is None
                assert ambiguous.reason_code == "AMBIGUOUS_AVAILABILITY_SCOPE"
                issues = list(await session.scalars(select(ParkingFieldIssue)))
                assert all(i.reason_code != "UNCONFIRMED_TIME_UNIT" for i in issues)
                payloads = list(await session.scalars(select(RawPayload)))
                assert len(payloads) == 4
                assert sum(p.payload_kind == "provider_response" for p in payloads) == 3
                report = next(p for p in payloads if p.part_key == "report")
                assert configured_store().load_json(report)["datasets"] == result["datasets"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_or_failed_methods_preserve_previous_facts_and_do_not_close_facilities(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = dict(
            zip(METHODS, ([facility(), facility("b")], [operation()], [live()]), strict=True)
        )
        try:
            assert (await collect(engine, rows))["accepted"] == 4
            async with factory() as session:
                a = await session.scalar(
                    select(ParkingSourceRecord).where(ParkingSourceRecord.provider_record_id == "a")
                )
                rid, old_operation = a.source_record_id, a.operation_payload_id
            rows["PrkSttusInfo"] = [facility(prk_plce_nm="new name")]
            result = await collect(engine, rows, failed={"PrkOprInfo"})
            assert (
                result["status"] == "partial"
                and result["datasets"]["PrkOprInfo"]["status"] == "failed"
            )
            async with factory() as session:
                a = await session.get(ParkingSourceRecord, rid)
                assert (
                    a.name == "new name"
                    and a.operation_payload_id == old_operation
                    and a.base_fee_krw == 1200
                )
                assert all(r.is_active for r in await session.scalars(select(ParkingSourceRecord)))
                assert a.base_minutes == 30 and a.base_usable
                assert not any(
                    i.field_path == "fees.base_minutes"
                    for i in await session.scalars(select(ParkingFieldIssue))
                )
            rows["PrkOprInfo"] = [operation(basic_info={"parking_chrge_bs_chrge": "2400"})]
            result = await collect(engine, rows, failed={"PrkSttusInfo", "PrkRealtimeInfo"})
            assert result["accepted"] == 1
            async with factory() as session:
                a = await session.get(ParkingSourceRecord, rid)
                assert a.base_fee_krw == 2400 and a.operation_payload_id != old_operation
                assert a.base_minutes is None and not a.base_usable
                previous = a.operation_payload_id
            rows["PrkOprInfo"] = []
            await collect(engine, rows)
            async with factory() as session:
                assert (
                    await session.get(ParkingSourceRecord, rid)
                ).operation_payload_id == previous
            # An old replay cannot relabel an operational value with a newer receipt.
            data = {
                "datasets": {
                    m: dict(
                        status="success",
                        complete=True,
                        provider_total=len(rows[m]),
                        error_code=None,
                        pages=[
                            dict(
                                page=1,
                                received_at="2020-01-01T00:00:00Z",
                                usable=True,
                                body={m: rows[m]},
                            )
                        ],
                    )
                    for m in METHODS
                }
            }
            data["datasets"]["PrkOprInfo"]["pages"][0]["body"]["PrkOprInfo"] = [operation()]
            async with factory.begin() as session:
                result = await save_national_parking(session, data)
            assert result["datasets"]["PrkOprInfo"]["rejection_reasons"] == {
                "older_operation_response": 1
            }
            async with factory() as session:
                assert (await session.get(ParkingSourceRecord, rid)).base_fee_krw == 2400
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_conflicting_facility_blocks_only_its_joins_and_all_failures_are_reported(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        rows = dict(
            zip(
                METHODS,
                ([facility(), facility("b")], [operation(), operation("b")], [live()]),
                strict=True,
            )
        )
        try:
            await collect(engine, rows)
            rows["PrkSttusInfo"].append(facility(prk_cmprt_co="20"))
            result = await collect(engine, rows)
            assert result["datasets"]["PrkSttusInfo"]["conflicts"] == 2
            assert result["datasets"]["PrkOprInfo"]["accepted"] == 1
            assert result["datasets"]["PrkOprInfo"]["rejection_reasons"] == {
                "conflicting_facility": 1
            }
            result = await collect(engine, rows, failed=set(METHODS))
            assert (
                result["status"] == "failed"
                and result["error_code"] == "parking_all_methods_failed"
            )
            assert result["received"] == result["accepted"] == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_typed_failure_rolls_back_all_methods_but_retains_page_evidence(
    normalized_database, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("do not expose connection details")

    monkeypatch.setattr(NationalParkingWriter, "attach", fail)

    async def run():
        engine = create_async_engine(normalized_database)
        rows = dict(zip(METHODS, ([facility()], [operation()], [live()]), strict=True))
        try:
            result = await collect(engine, rows)
            assert result["status"] == "failed" and result["error_code"] == "normalization_failed"
            async with async_sessionmaker(engine)() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 0
                )
                assert await session.scalar(select(func.count()).select_from(RawPayload)) == 4
                stored = await session.scalar(select(CollectionRun))
                assert stored.rejected == 3 and stored.accepted == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
