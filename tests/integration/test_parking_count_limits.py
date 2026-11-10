"""Out-of-range source rows cannot roll back good updates or retire stored facilities."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    CollectionRun,
    ParkingAvailabilityLatest,
    ParkingFacility,
    ParkingSourceRecord,
)
from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.collection import CollectionRepository
from tests.fixtures.citydata import boundary, document
from tests.fixtures.citydata import collect as collect_city
from tests.integration.test_national_parking_storage import collect as collect_national
from tests.integration.test_national_parking_storage import facility, live, operation

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]
REASON = "parking_count_out_of_range"
OVERFLOW = str(2**31)


async def snapshot(factory):
    async with factory() as session:
        records = {}
        for row in await session.scalars(select(ParkingSourceRecord)):
            records[row.provider_record_id] = {
                key: getattr(row, key)
                for key in (
                    "source_record_id",
                    "parking_id",
                    "registered_capacity",
                    "collected_at",
                    "processed_at",
                    "updated_at",
                    "is_active",
                    "base_fee_krw",
                    "operation_received_at",
                )
            }
        observations = {
            row.source_record_id: {
                col.name: getattr(row, col.name) for col in row.__table__.columns
            }
            for row in await session.scalars(select(ParkingAvailabilityLatest))
        }
        assert all(row.is_active for row in await session.scalars(select(ParkingFacility)))
        return records, observations


def assert_partial(result, *, accepted=1, rejected=2):
    assert result["status"] == "partial", result
    assert result["error_code"] is None
    assert (result["accepted"], result["rejected"]) == (accepted, rejected)
    assert result["rejection_reasons"] == {REASON: rejected}


@pytest.mark.parametrize(
    "source,field",
    [
        (SOURCE, "TPKCT"),
        (MUNICIPAL_PARKING_SOURCE, "TPKCT"),
        (MUNICIPAL_PARKING_SOURCE, "NOW_PRK_VHCL_CNT"),
    ],
)
def test_seoul_rejects_only_bad_rows_and_preserves_absent_facilities(
    normalized_database, source, field
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        stamp = datetime.now(UTC) - timedelta(minutes=2)
        initial = [
            dict(
                PKLT_CD=code,
                PKLT_NM=code,
                LAT=37.56,
                LOT=126.97,
                TPKCT="10",
                PRK_STTS_YN="1",
                NOW_PRK_VHCL_CNT="5",
                NOW_PRK_VHCL_UPDT_TM=stamp.isoformat(),
            )
            for code in ("good", "bad", "absent")
        ]

        async def send(rows):
            async def fetch():
                return rows

            return await run_parking(engine, fetch, source)

        try:
            assert (await send(initial))["accepted"] == 3
            before, live_before = await snapshot(factory)
            bad = initial[1] | {field: OVERFLOW}
            new_bad = bad | {"PKLT_CD": "new-bad", "PKLT_NM": "new-bad"}
            good = initial[0] | {
                "TPKCT": "20",
                "NOW_PRK_VHCL_UPDT_TM": (stamp + timedelta(seconds=1)).isoformat(),
            }
            result = await send([good, bad, new_bad])
            assert_partial(result)
            after, live_after = await snapshot(factory)
            assert set(after) == set(before)
            assert after["good"]["registered_capacity"] == 20
            for code in ("bad", "absent"):
                assert after[code] == before[code]
                rid = before[code]["source_record_id"]
                assert live_after.get(rid) == live_before.get(rid)
            async with factory() as session:
                stored = await session.get(CollectionRun, UUID(result["run_id"]))
                reasons = await CollectionRepository(session).rejection_counts([stored.id])
                assert stored.status == "partial" and reasons[stored.id] == {REASON: 2}
                assert (stored.accepted, stored.rejected) == (1, 2)
            # If every row is bad there is no update, including no closure or new attempt.
            failed = await send([bad, new_bad])
            assert failed["status"] == "failed" and failed["error_code"] == "no_valid_records"
            assert failed["rejection_reasons"] == {REASON: 2}
            assert await snapshot(factory) == (after, live_after)
            # A subsequent valid reading can still replace the retained old value.
            recovered = await send([good, initial[1] | {"TPKCT": "30"}, initial[2]])
            assert recovered["status"] == "success" and recovered["accepted"] == 3
            assert (await snapshot(factory))[0]["bad"]["registered_capacity"] == 30
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["CPCTY", "CUR_PRK_CNT"])
def test_citydata_keeps_other_parking_and_population_and_reports_specific_reason(
    normalized_database, field
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        data = document()
        base = data["CITYDATA"]["PRK_STTS"][0]
        initial = [base | {"PRK_CD": code, "PRK_NM": code} for code in ("good", "bad", "absent")]
        data["CITYDATA"]["PRK_STTS"] = initial
        try:
            await boundary(factory)
            assert (await collect_city(engine, data))["parking_storage"]["accepted"] == 3
            before, live_before = await snapshot(factory)
            bad = initial[1] | {field: OVERFLOW}
            changed = deepcopy(data)
            changed["CITYDATA"]["PRK_STTS"] = [
                initial[0] | {"CPCTY": "20", "CUR_PRK_TIME": "2026-11-04 09:01"},
                bad,
                bad | {"PRK_CD": "new-bad", "PRK_NM": "new-bad"},
            ]
            changed["CITYDATA"]["LIVE_PPLTN_STTS"][0]["PPLTN_TIME"] = "2026-11-04 09:01"
            result = await collect_city(engine, changed, 10)
            assert_partial(result["parking_storage"])
            assert result["status"] == "partial" and result["population_quality"]["updated"]
            after, live_after = await snapshot(factory)
            assert set(after) == set(before)
            assert after["good"]["registered_capacity"] == 20
            for code in ("bad", "absent"):
                assert after[code] == before[code]
                rid = before[code]["source_record_id"]
                assert live_after[rid] == live_before[rid]
            async with factory() as session:
                stored = await session.get(CollectionRun, UUID(result["run_id"]))
                reasons = await CollectionRepository(session).rejection_counts([stored.id])
                assert reasons[stored.id].get(REASON) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_national_bad_facility_does_not_update_its_operations_or_close_others(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = {
            "PrkSttusInfo": [facility(code) for code in ("good", "bad", "absent")],
            "PrkOprInfo": [operation("bad")],
            "PrkRealtimeInfo": [live("bad")],
        }
        try:
            await collect_national(engine, rows)
            before, live_before = await snapshot(factory)
            rows["PrkSttusInfo"] = [
                facility("good", prk_cmprt_co="20"),
                facility("bad", prk_cmprt_co=OVERFLOW),
                facility("new-bad", prk_cmprt_co=OVERFLOW),
            ]
            result = await collect_national(engine, rows)
            assert result["status"] == "partial" and result["error_code"] is None
            assert_partial(result["datasets"]["PrkSttusInfo"])
            for method in ("PrkOprInfo", "PrkRealtimeInfo"):
                assert result["datasets"][method]["rejection_reasons"] == {
                    "conflicting_facility": 1
                }
            after, live_after = await snapshot(factory)
            assert set(after) == set(before)
            assert after["good"]["registered_capacity"] == 20
            for code in ("bad", "absent"):
                assert after[code] == before[code]
            assert live_after == live_before
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["pkfc_ParkingLots_total", "pkfc_Available_ParkingLots_total"])
def test_national_overflow_attempt_does_not_discard_other_live_updates(normalized_database, field):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = {
            "PrkSttusInfo": [facility(code) for code in ("good", "bad", "new-live")],
            "PrkOprInfo": [],
            "PrkRealtimeInfo": [live("good"), live("bad")],
        }
        try:
            await collect_national(engine, rows)
            before, live_before = await snapshot(factory)
            rows = {
                "PrkSttusInfo": [],
                "PrkOprInfo": [],
                "PrkRealtimeInfo": [
                    live("good", "5"),
                    live("bad") | {field: OVERFLOW},
                    live("new-live") | {field: OVERFLOW},
                ],
            }
            result = await collect_national(engine, rows)
            assert result["status"] == "partial" and result["error_code"] is None
            assert_partial(result["datasets"]["PrkRealtimeInfo"])
            after, live_after = await snapshot(factory)
            assert after == before
            good, bad, new = (
                before[code]["source_record_id"] for code in ("good", "bad", "new-live")
            )
            assert live_after[good]["attempt_reported_available_spaces"] == 5
            assert live_after[bad] == live_before[bad]
            assert new not in live_after
            async with factory() as session:
                stored = await session.get(CollectionRun, UUID(result["run_id"]))
                reasons = await CollectionRepository(session).rejection_counts([stored.id])
                assert reasons[stored.id] == {"PrkRealtimeInfo:" + REASON: 2}
        finally:
            await engine.dispose()

    asyncio.run(run())
