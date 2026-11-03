import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, ParkingSourceRecord, RawPayload
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.parking import ParkingRepository

pytestmark = pytest.mark.integration


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
                assert await s.scalar(select(func.count()).select_from(RawPayload)) == 3
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


def test_municipal_pipeline_links_only_unique_active_positions_and_preserves_raw(
    normalized_database, monkeypatch
):
    from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
    from app.modules.data_pipeline.pipelines import snapshots
    from app.modules.data_pipeline.pipelines.parking import run_municipal_parking
    from app.storage.payloads import configured_store

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
                original_payload = await session.scalar(
                    select(RawPayload)
                    .join(CollectionRun)
                    .where(
                        CollectionRun.source == MUNICIPAL_PARKING_SOURCE,
                        RawPayload.document_type == "object",
                    )
                )
                assert configured_store().load_json(original_payload) == {"rows": rows}

            def fail(*args, **kwargs):
                raise RuntimeError("private database error")

            monkeypatch.setattr(snapshots, "normalize_snapshot", fail)
            rows[0]["TPKCT"] = "999"
            failed = await run_municipal_parking(engine, municipal)
            assert failed["status"] == "failed" and failed["error_code"] == "normalization_failed"
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
