"""Current sections are independent; failures retain DB values without replaying input."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, ParkingSourceRecord, PopulationAttemptLatest, PopulationLatest
from app.modules.data_pipeline.pipelines import citydata_storage
from tests.fixtures.citydata import boundary, collect, document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


def test_population_sql_failure_does_not_discard_current_parking(normalized_database, monkeypatch):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            assert (await collect(engine, document()))["status"] == "success"
            original = citydata_storage.normalize_population

            def fail_after_write(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("private database detail")

            monkeypatch.setattr(citydata_storage, "normalize_population", fail_after_write)
            data = document()
            data["CITYDATA"]["LIVE_PPLTN_STTS"][0]["AREA_PPLTN_MAX"] = "20"
            data["CITYDATA"]["PRK_STTS"][0]["CPCTY"] = "30"
            result = await collect(engine, data, 1)
            assert result["status"] == "partial"
            assert result["parking_storage"]["accepted"] == 1
            assert result["error_code"] == "population_normalization_failed"
            async with factory() as session:
                assert (
                    await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
                ).max_people == 10
                assert (await session.scalar(select(ParkingSourceRecord))).registered_capacity == 30
                assert (await session.get(PopulationAttemptLatest, "POI001")).status == "failed"
                assert (
                    await session.get(CollectionRun, UUID(result["run_id"]))
                ).status == "partial"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("section", [None, [], [{"PRK_CD": "invalid"}], [None]])
def test_bad_or_empty_parking_keeps_stored_values_without_reprocessing(
    normalized_database, section
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            await collect(engine, document())
            async with factory() as session:
                record = await session.scalar(select(ParkingSourceRecord))
                previous = (record.source_record_id, record.processed_at, record.collected_at)
            data = document()
            if section is None:
                data["CITYDATA"].pop("PRK_STTS")
            else:
                data["CITYDATA"]["PRK_STTS"] = section
            result = await collect(engine, data, 10)
            assert result["population_quality"]["updated"]
            assert result["parking_storage"]["accepted"] == 0
            async with factory() as session:
                record = await session.scalar(select(ParkingSourceRecord))
                assert (
                    record.source_record_id,
                    record.processed_at,
                    record.collected_at,
                ) == previous
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_parking_row_does_not_discard_other_rows(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            data = document()
            data["CITYDATA"]["PRK_STTS"].append(None)
            result = await collect(engine, data)
            assert result["status"] == "partial"
            assert result["parking_storage"]["accepted"] == 1
            assert result["parking_storage"]["rejected"] == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
