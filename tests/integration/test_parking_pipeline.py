import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.db.session import create_engine
from app.models.parking import CollectionRun, ParkingRecord
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.parking import ParkingRepository


@pytest.mark.integration
def test_atomic_runs_idempotency_spatial_lookup_and_failure_preservation():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")

    async def check():
        engine = create_engine(Settings(_env_file=None, database_url=url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        source = f"test_{uuid4().hex}"
        good = {"PKLT_CD": "001", "PKLT_NM": "test parking", "LAT": 37.5665, "LOT": 126.978}

        async def fetch():
            return [good, good, {**good, "LAT": 0}]

        async def fail():
            raise CollectionError("test_failure")

        try:
            first = await run_parking(engine, fetch, source)
            assert first["status"] == "partial"
            assert (first["accepted"], first["duplicates"], first["rejected"]) == (1, 1, 1)
            second = await run_parking(engine, fetch, source)
            assert second["status"] == "partial"
            async with factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(ParkingRecord)
                    .where(ParkingRecord.source == source)
                )
                assert count == 1
                nearby = await ParkingRepository(session).nearby(37.5665, 126.978, source, 20)
                assert nearby["radius_m"] == 300
                assert nearby["items"][0]["distance_m"] == 0
                original = nearby["items"][0]["collected_at"]
            failed = await run_parking(engine, fail, source)
            assert failed["status"] == "failed"
            async with factory() as session:
                nearby = await ParkingRepository(session).nearby(37.5665, 126.978, source, 20)
                assert nearby["items"][0]["collected_at"] == original
                outside = await ParkingRepository(session).nearby(37.7, 127.2, source, 20)
                assert outside["status"] == "unconfirmed"
            async with engine.connect() as lock:
                await lock.execute(
                    text("SELECT pg_advisory_lock(hashtext(:source))"), {"source": source}
                )
                try:
                    skipped = await run_parking(engine, fetch, source)
                    assert skipped["status"] == "skipped"
                finally:
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
                    )
        finally:
            async with factory.begin() as session:
                await session.execute(delete(ParkingRecord).where(ParkingRecord.source == source))
                await session.execute(delete(CollectionRun).where(CollectionRun.source == source))
            await engine.dispose()

    asyncio.run(check())
