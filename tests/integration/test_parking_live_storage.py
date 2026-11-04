"""B04 typed facts and clocks exercised against disposable PostGIS databases."""

import asyncio
from datetime import UTC, datetime, time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ParkingAvailabilityLatest, ParkingFieldIssue, ParkingSourceRecord
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
    normalize_parking,
)
from app.modules.data_pipeline.normalizers.parking_resolution import ParkingCandidate
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.parking import ParkingRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.services.parking_registration import registered_information

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


def city(**changes):
    return {
        "PRK_CD": "1",
        "PRK_NM": "주차장",
        "LAT": 37.56,
        "LNG": 126.97,
        "CPCTY": "10",
        "CUR_PRK_YN": "Y",
        "CUR_PRK_CNT": "0",
        "CUR_PRK_TIME": "2020-01-01T08:00:00Z",
        **changes,
    }


def stamp(hour):
    return datetime(2020, 1, 1, hour, tzinfo=UTC)


async def write(factory, row, receipt):
    async with factory.begin() as session:
        repo = CollectionRepository(session)
        run = await repo.start(CITYDATA_PARKING_SOURCE, "all")
        candidate = ParkingCandidate(
            normalize_parking(row, receipt, CITYDATA_PARKING_SOURCE),
            receipt,
            "test",
        )
        result = await session.run_sync(
            lambda sync: NormalizedWriter(sync, Base.metadata).parking([candidate], complete=False)
        )
        assert result["accepted"] == 1
        await repo.finish(run, status="success", received=1, accepted=1)
        return receipt


def test_conditions_hours_quality_and_reader_roundtrip(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = {
            "PKLT_CD": "1",
            "PKLT_NM": "주차장",
            "LAT": 37.56,
            "LOT": 126.97,
            "TPKCT": "0",
            "CHGD_FREE_SE": "Y",
            "CHGD_FREE_NM": "유료",
            "NGHT_FREE_OPN_YN": "N",
            "SAT_CHGD_FREE_SE": "N",
            "LHLDY_YN": "?",
            "PKLT_KND": "NW",
            "PKLT_KND_NM": "노외",
            "OPER_SE": "1",
            "OPER_SE_NM": "시간제",
            "TELNO": "02-0000-0000",
            "CRB_PKLT_MNG_GROUP_NO": "group",
            "PRK_NOW_INFO_PVSN_YN": "0",
            "PRK_NOW_INFO_PVSN_YN_NM": "미연계",
            "PRK_HM": "5",
            "PRK_CRG": "0",
            "WD_OPER_BGNG_TM": "0000",
            "WD_OPER_END_TM": "2400",
            "WE_OPER_BGNG_TM": "0900",
            "WE_OPER_END_TM": "1800",
            "LHLDY_BGNG": "2561",
            "LHLDY": "1800",
            "LAST_DATA_SYNC_TM": "2020-01-01T08:00:00Z",
        }

        async def fetch():
            return [row]

        try:
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 1
            async with factory() as session:
                record = await session.scalar(select(ParkingSourceRecord))
                assert record.is_paid is True and record.night_free_open is False
                assert record.saturday_is_paid is False and record.holiday_is_paid is None
                assert record.holiday_payment_code == "?"
                assert (record.parking_type_name, record.operation_name, record.phone) == (
                    "노외",
                    "시간제",
                    "02-0000-0000",
                )
                assert record.management_group_id == "group"
                assert record.realtime_link_code == "0" and record.base_usable
                assert record.base_fee_krw == 0 and record.reason_code is None
                records = await ParkingDetailRepository(session).list_records(record.parking_id)
                hours = {h.day_type: h for h in records[0].operating_hours}
                assert (
                    hours["weekday"].opens_at,
                    hours["weekday"].closes_at,
                    hours["weekday"].end_day_offset,
                ) == (time(0), time(0), 1)
                assert hours["holiday"].state == "invalid"
                assert hours["holiday"].reason_code == "INVALID_HHMM"
                result = registered_information(records[0])
                assert result["reason_code"] is None
                assert result["value"]["operating_hours"]["saturday"] is None
                assert "주말 09:00~18:00" in result["value"]["operating_hours"]["notes"]
                issues = {
                    i.field_path: i.reason_code
                    for i in await session.scalars(select(ParkingFieldIssue))
                }
                assert issues["conditions.holiday_is_paid"] == "INVALID_FLAG"
                assert issues["operating_hours.holiday"] == "INVALID_HHMM"
                assert (
                    await ParkingRepository(session).availability(record.source_record_id) is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes,status,available",
    [
        ({}, "accepted", 0),
        ({"CUR_PRK_CNT": "10"}, "accepted", 10),
        ({"CPCTY": "0"}, "accepted", 0),
        ({"CUR_PRK_YN": "N"}, "not_provided", None),
        ({"CUR_PRK_YN": "?"}, "unknown", None),
        ({"CUR_PRK_CNT": ""}, "invalid", None),
        ({"CUR_PRK_CNT": "11"}, "invalid", None),
        ({"CUR_PRK_TIME": "2999-01-01T08:00:00Z"}, "invalid", None),
    ],
)
def test_reported_counts_do_not_turn_vacancies_into_occupied_counts(
    normalized_database, changes, status, available
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await write(factory, city(**changes), stamp(10))
            async with factory() as session:
                live = await session.scalar(select(ParkingAvailabilityLatest))
                assert live.attempt_status == status
                assert live.reported_available_spaces == available
                assert live.occupied_spaces is live.calculated_available_spaces is None
                assert live.usable == (status == "accepted")
                assert live.method == "reported" and live.scope == "provider_record"
                if status != "accepted":
                    assert live.observed_at is live.total_spaces is None
                    assert live.attempt_reason_code
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_last_good_observation_and_last_attempt_have_independent_clocks(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)

        async def state():
            async with factory() as session:
                return await session.scalar(select(ParkingAvailabilityLatest))

        try:
            good = await write(factory, city(), stamp(10))
            await write(factory, city(CUR_PRK_YN="N"), stamp(11))
            value = await state()
            assert (value.received_at, value.observed_at, value.reported_available_spaces) == (
                good,
                stamp(8),
                0,
            )
            assert value.usable and value.attempt_status == "not_provided"
            await write(
                factory, city(CUR_PRK_TIME="2020-01-01T07:00:00Z", CUR_PRK_CNT="5"), stamp(12)
            )
            value = await state()
            assert value.received_at == good and value.attempt_status == "stale"
            await write(factory, city(CUR_PRK_CNT="5"), stamp(13))
            value = await state()
            assert value.received_at == good and not value.usable
            assert value.reason_code == "SAME_TIME_CONFLICT" and value.attempt_status == "conflict"
            # Replay of one side cannot silently resolve a same-time conflict.
            await write(factory, city(), stamp(13))
            assert not (await state()).usable
            # Observation can advance even when its response arrived before the latest attempt.
            newer = city(CUR_PRK_TIME="2020-01-01T09:00:00Z", CUR_PRK_CNT="2")
            receipt = await write(factory, newer, stamp(11))
            value = await state()
            assert value.received_at == receipt and value.observed_at == stamp(9) and value.usable
            assert value.attempt_received_at == stamp(13) and value.attempt_status == "conflict"
            assert value.attempt_reported_available_spaces == 0  # Most recent conflicting input.
            await write(factory, newer, stamp(14))
            value = await state()
            assert value.received_at == receipt and value.received_at == stamp(11)
            assert value.attempt_received_at == stamp(14) and value.attempt_status == "accepted"
            async with factory() as session:
                issues = list(
                    await session.scalars(
                        select(ParkingFieldIssue).where(
                            ParkingFieldIssue.field_path.startswith("availability")
                        )
                    )
                )
                assert issues == []
                registration = await session.scalar(select(ParkingSourceRecord))
                assert registration.collected_at == stamp(14)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_municipal_calculation_and_ambiguous_scope_are_not_duplicated(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        first = {
            "PKLT_CD": "1",
            "PKLT_NM": "도로 구간",
            "LAT": 37.56,
            "LOT": 126.97,
            "TPKCT": "10",
            "PRK_STTS_YN": "1",
            "NOW_PRK_VHCL_CNT": "10",
            "NOW_PRK_VHCL_UPDT_TM": "2020-01-01T08:00:00Z",
        }
        rows = [first]

        async def fetch():
            return rows

        try:
            assert (await run_parking(engine, fetch, MUNICIPAL_PARKING_SOURCE))["accepted"] == 1
            async with factory() as session:
                live = await session.scalar(select(ParkingAvailabilityLatest))
                assert live.usable and live.calculated_available_spaces == 0
                assert live.occupied_spaces == 10 and live.reported_available_spaces is None
            # A second space of an already known code is held by B03, not guessed.
            # Two new spaces arriving together can be registered, but not assigned duplicate totals.
            rows = [dict(first, PKLT_CD="2"), dict(first, PKLT_CD="2", LAT=37.57)]
            assert (await run_parking(engine, fetch, MUNICIPAL_PARKING_SOURCE))["accepted"] == 2
            async with factory() as session:
                live = list(
                    await session.scalars(
                        select(ParkingAvailabilityLatest).where(
                            ParkingAvailabilityLatest.scope_key == "2"
                        )
                    )
                )
                assert len(live) == 2 and all(not r.usable for r in live)
                assert all(r.attempt_reason_code == "AMBIGUOUS_AVAILABILITY_SCOPE" for r in live)
                assert all(r.calculated_available_spaces is None for r in live)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_batch_conflicts_reach_attempt_state_without_overwriting_registration(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = [city()]

        async def fetch():
            return rows

        try:
            assert (await run_parking(engine, fetch, CITYDATA_PARKING_SOURCE))["accepted"] == 1
            async with factory() as session:
                old = await session.scalar(select(ParkingAvailabilityLatest))
                receipt = old.received_at
            rows = [city(), city(CUR_PRK_CNT="5")]
            result = await run_parking(engine, fetch, CITYDATA_PARKING_SOURCE)
            assert result["conflicts"] == 2 and result["accepted"] == 0
            async with factory() as session:
                live = await session.scalar(select(ParkingAvailabilityLatest))
                assert live.received_at == receipt and live.reported_available_spaces == 0
                assert not live.usable and live.attempt_status == "conflict"
                assert live.reason_code == "SAME_TIME_CONFLICT"
        finally:
            await engine.dispose()

    asyncio.run(run())
