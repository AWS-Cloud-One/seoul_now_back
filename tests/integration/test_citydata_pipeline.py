"""Direct CITYDATA collection rejects bad envelopes and keeps successful sections/cycles."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, ParkingSourceRecord, PopulationAttemptLatest, PopulationLatest
from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.pipelines import citydata
from app.repositories.normalization import NormalizedWriter
from tests.fixtures.citydata import STAMP, boundary, collect, document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


@pytest.mark.parametrize(
    "change,error",
    [
        ({"RESULT": {"CODE": "INFO-100"}}, "provider_auth_error"),
        ({"RESULT": {"CODE": "ERROR-337"}}, "provider_quota_exceeded"),
        ({"RESULT": None}, "provider_invalid_response"),
        ({"CITYDATA": {"AREA_CD": "POI002", "AREA_NM": "test"}}, "area_identity_mismatch"),
    ],
)
def test_rejected_envelope_preserves_good_observation(normalized_database, change, error):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            await collect(engine, document())
            result = await collect(engine, {**document(), **change}, 1)
            expected = "paused_provider_limit" if error in citydata.STOP_ERRORS else "failed"
            assert result["status"] == expected and result["error_code"] == error
            async with factory() as session:
                value = await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert value.received_at == STAMP and value.max_people == 10
                attempt = await session.get(PopulationAttemptLatest, "POI001")
                assert attempt.status == "failed" and attempt.error_code == error
                assert (await session.scalar(select(ParkingSourceRecord))).collected_at == STAMP
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_batch_stops_shared_provider_error_and_stores_already_received_parking(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(citydata.asyncio, "sleep", AsyncMock())

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        targets = {"POI001": "first", "POI002": "second", "POI003": "third"}
        called = []

        async def fetch(code, name):
            called.append(code)
            if code == "POI002":
                raise CollectionError("provider_quota_exceeded")
            return CityDataDocument(document(code, name), STAMP)

        try:
            await boundary(factory, targets)
            result = await citydata.run_citydata_batch(engine, fetch, targets)
            assert result["status"] == "paused_provider_limit"
            assert called == ["POI001", "POI002"] and result["remaining"] == 1
            assert result["parking_storage"]["accepted"] == 1
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(PopulationLatest)) == 1
                assert (
                    await session.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 1
                )
                assert (
                    await session.get(PopulationAttemptLatest, "POI002")
                ).error_code == "provider_quota_exceeded"
                assert await session.get(PopulationAttemptLatest, "POI003") is None
                runs = list(await session.scalars(select(CollectionRun)))
                assert len(runs) == 1
                assert str(runs[0].id) == result["run_id"] == result["parking_storage"]["run_id"]
                assert runs[0].accepted == 2 and runs[0].received == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parking_database_failure_rolls_back_all_rows_and_keeps_population(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        original = NormalizedWriter.parking

        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private details")

        monkeypatch.setattr(NormalizedWriter, "parking", fail_after_write)
        try:
            await boundary(factory)
            data = document()
            data["CITYDATA"]["PRK_STTS"].append(dict(data["CITYDATA"]["PRK_STTS"][0], PRK_CD="2"))
            result = await collect(engine, data)
            assert result["status"] == "failed" and result["parking_storage"]["accepted"] == 0
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 0
                )
                assert await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
                run = await session.get(CollectionRun, UUID(result["parking_storage"]["run_id"]))
                assert run.status == "failed" and run.rejected == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
