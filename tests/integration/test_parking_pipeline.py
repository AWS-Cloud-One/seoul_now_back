import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.clients.seoul import CollectionError
from app.models import CollectionRun, ParkingSourceRecord
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.parking import ParkingRepository
from app.services.parking_registration import registered_information

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


def test_partial_municipal_response_keeps_successes_and_does_not_close_missing_facilities(
    normalized_database,
):
    from datetime import UTC, datetime, timedelta

    from app.modules.data_pipeline.collectors.parking import ParkingSnapshot
    from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
    from app.modules.data_pipeline.pipelines.parking import run_municipal_parking

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        registered = [
            {"PKLT_CD": code, "PKLT_NM": code, "LAT": 37.56, "LOT": 126.97} for code in ("a", "b")
        ]
        receipt = datetime(2026, 11, 5, tzinfo=UTC)
        rows = [{"PKLT_CD": code, "PKLT_NM": code, "TPKCT": "100"} for code in ("a", "b")]

        async def reference():
            return registered

        async def initial():
            return ParkingSnapshot(rows, 2, receipt, True)

        async def partial():
            return ParkingSnapshot(
                [{**rows[0], "TPKCT": "150"}, None],
                3,
                receipt + timedelta(minutes=10),
                False,
                "provider_server_error",
            )

        try:
            assert (await run_parking(engine, reference, SOURCE))["status"] == "success"
            assert (await run_municipal_parking(engine, initial))["accepted"] == 2
            result = await run_municipal_parking(engine, partial)
            assert (result["status"], result["accepted"], result["rejected"]) == ("partial", 1, 1)
            async with factory() as session:
                records = {
                    r.provider_record_id: r
                    for r in await session.scalars(
                        select(ParkingSourceRecord).where(
                            ParkingSourceRecord.source == MUNICIPAL_PARKING_SOURCE
                        ),
                    )
                }
                assert records["a"].registered_capacity == 150
                assert records["b"].registered_capacity == 100
                assert records["b"].collected_at == receipt
                assert all(record.is_active for record in records.values())
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(CollectionRun)
                        .where(
                            CollectionRun.source == MUNICIPAL_PARKING_SOURCE,
                        )
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,fees",
    [
        (
            "seoul_getparkinfo",
            {
                "PRK_HM": "5",
                "PRK_CRG": "430",
                "ADD_UNIT_TM_MNT": "10",
                "ADD_CRG": "800",
                "DLY_MAX_CRG": "20000",
                "LAST_DATA_SYNC_TM": "2020-01-01 09:00",
            },
        ),
        (
            "seoul_municipal_parking",
            {
                "BSC_PRK_HR": "5",
                "BSC_PRK_CRG": "430",
                "ADD_PRK_HR": "10",
                "ADD_PRK_CRG": "800",
                "DAY_MAX_CRG": "20000",
                "NOW_PRK_VHCL_UPDT_TM": "2020-01-01 09:00",
            },
        ),
        (
            "seoul_citydata_prk",
            {
                "TIME_RATES": "5",
                "RATES": "430",
                "ADD_TIME_RATES": "10",
                "ADD_RATES": "800",
                "CUR_PRK_TIME": "2020-01-01 09:00",
            },
        ),
    ],
)
def test_source_fee_mapping_and_hours_survive_typed_storage(normalized_database, source, fees):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)

        async def fetch():
            return [
                {
                    "PKLT_CD": "1",
                    "PKLT_NM": "주차장",
                    "PRK_CD": "1",
                    "PRK_NM": "주차장",
                    "LAT": 37.56,
                    "LOT": 126.97,
                    "LNG": 126.97,
                    "TPKCT": "50",
                    "CPCTY": "50",
                    "WD_OPER_BGNG_TM": "0000",
                    "WD_OPER_END_TM": "2400",
                    "WE_OPER_BGNG_TM": "0900",
                    "WE_OPER_END_TM": "1800",
                    **fees,
                }
            ]

        try:
            assert (await run_parking(engine, fetch, source))["status"] == "success"
            async with factory() as session:
                record = await session.scalar(
                    select(ParkingSourceRecord).options(
                        selectinload(ParkingSourceRecord.operating_hours)
                    )
                )
                assert (
                    record.base_minutes,
                    record.base_fee_krw,
                    record.additional_minutes,
                    record.additional_fee_krw,
                ) == (5, 430, 10, 800)
                assert record.daily_max_krw == (None if source == "seoul_citydata_prk" else 20000)
                assert (record.source_updated_at is not None) == (source == "seoul_getparkinfo")
                assert record.base_usable and record.additional_usable
                hours = registered_information(record)["value"]["operating_hours"]
                if source != "seoul_citydata_prk":
                    assert hours["weekdays"] == "0000~2400"
                    assert hours["saturday"] is hours["sunday_holidays"] is None
                    assert "주말 09:00~18:00" in hours["notes"]
                    assert {h.day_type for h in record.operating_hours} == {
                        "weekday",
                        "weekend",
                        "holiday",
                    }
                else:
                    assert hours is None and record.operating_hours == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_atomic_runs_idempotency_spatial_lookup_and_failure_preservation(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        source = "seoul_getparkinfo"
        good = {
            "PKLT_CD": "001",
            "PKLT_NM": "주차장",
            "LAT": 37.5665,
            "LOT": 126.978,
            "PRK_CRG": "0",
        }

        async def fetch():
            return [good, good, {**good, "LAT": 0}]

        async def fail():
            raise CollectionError("test_failure")

        try:
            first = await run_parking(engine, fetch, source)
            assert (first["status"], first["accepted"], first["duplicates"], first["rejected"]) == (
                "partial",
                1,
                1,
                1,
            )
            assert (await run_parking(engine, fetch, source))["status"] == "partial"
            good["TPKCT"] = "50"
            assert (await run_parking(engine, fetch, source))["status"] == "partial"
            async with factory() as s:
                records = list(await s.scalars(select(ParkingSourceRecord)))
                assert (
                    len(records) == 1
                    and records[0].registered_capacity == 50
                    and records[0].base_fee_krw == 0
                )
                assert await s.scalar(select(func.count()).select_from(CollectionRun)) == 3
                assert await ParkingRepository(s).active_positions(source) == {
                    "001": {(37.5665, 126.978)}
                }
                distance = await s.scalar(
                    select(
                        func.ST_Distance(
                            ParkingSourceRecord.location,
                            func.ST_GeogFromText("SRID=4326;POINT(126.978 37.5665)"),
                        )
                    )
                )
                assert distance == 0
                original = records[0].collected_at
            assert (await run_parking(engine, fail, source))["status"] == "failed"
            async with factory() as s:
                record = await s.scalar(select(ParkingSourceRecord))
                assert record.collected_at == original
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(ParkingSourceRecord)
                        .where(
                            func.ST_DWithin(
                                ParkingSourceRecord.location,
                                func.ST_GeogFromText("SRID=4326;POINT(127.2 37.7)"),
                                1000,
                            )
                        )
                    )
                    == 0
                )
                assert (
                    await s.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()))
                ).status == "failed"
            async with engine.connect() as lock:
                await lock.execute(
                    text("SELECT pg_advisory_lock(hashtext(:source))"), {"source": source}
                )
                try:
                    assert (await run_parking(engine, fetch, source))["status"] == "skipped"
                finally:
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_municipal_pipeline_links_unique_positions_and_records_one_run(
    normalized_database, monkeypatch
):
    from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
    from app.modules.data_pipeline.pipelines.parking import run_municipal_parking
    from app.repositories.normalization import NormalizedWriter

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        registrations = [
            {"PKLT_CD": code, "PKLT_NM": code, "LAT": lat, "LOT": 127}
            for code, lat in (
                ("unique", 37.5),
                ("ambiguous", 37.5),
                ("ambiguous", 37.6),
                ("inactive", 37.5),
            )
        ]
        rows = [
            {"PKLT_CD": code, "PKLT_NM": code, "TPKCT": "100"}
            for code in ("unique", "ambiguous", "inactive", "missing")
        ]

        async def registered():
            return registrations

        async def municipal():
            return rows

        try:
            assert (await run_parking(engine, registered, SOURCE))["status"] == "success"
            async with factory.begin() as session:
                inactive = await session.scalar(
                    select(ParkingSourceRecord).where(
                        ParkingSourceRecord.source == SOURCE,
                        ParkingSourceRecord.provider_record_id == "inactive",
                    )
                )
                inactive.is_active = False
            result = await run_municipal_parking(engine, municipal)
            assert (result["status"], result["accepted"], result["rejected"]) == ("partial", 1, 3)
            assert all("LAT" not in row and "LOT" not in row for row in rows)
            async with factory() as session:
                records = list(
                    await session.scalars(
                        select(ParkingSourceRecord).where(
                            ParkingSourceRecord.source == MUNICIPAL_PARKING_SOURCE
                        )
                    )
                )
                assert len(records) == 1 and records[0].provider_record_id == "unique"
                assert records[0].registered_capacity == 100
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(CollectionRun)
                        .where(CollectionRun.source == MUNICIPAL_PARKING_SOURCE)
                    )
                    == 1
                )

            def fail(*args, **kwargs):
                raise RuntimeError("private database error")

            monkeypatch.setattr(NormalizedWriter, "parking", fail)
            rows[0]["TPKCT"] = "999"
            failed = await run_municipal_parking(engine, municipal)
            assert failed["status"] == "failed" and failed["error_code"] == "internal_error"
            async with factory() as session:
                record = await session.scalar(
                    select(ParkingSourceRecord).where(
                        ParkingSourceRecord.source == MUNICIPAL_PARKING_SOURCE
                    )
                )
                assert record.registered_capacity == 100
        finally:
            await engine.dispose()

    asyncio.run(run())
