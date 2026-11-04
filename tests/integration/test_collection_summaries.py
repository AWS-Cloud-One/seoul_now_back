"""Bounded summaries cannot erase current values, attempts or publication recovery state."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, MapPopulationObservation, MapRoadObservation, PopulationLatest
from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.pipelines import citydata, topis
from app.modules.data_pipeline.pipelines.maintenance import recover_collection
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.repositories.collection import CollectionRepository
from app.repositories.map import MapRepository
from app.repositories.reference import ReferenceRepository
from jobs.scheduler import recover_publication
from tests.fixtures.citydata import boundary, document
from tests.fixtures.topis import response, seed_roads

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_runtime_files")]


@pytest.mark.parametrize(
    "layer,source", [("population", "seoul_citydata"), ("roads", "topis_traffic")]
)
def test_pruning_keeps_last_good_value_and_failure_after_all_origin_logs_expire(
    normalized_database, monkeypatch, layer, source
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "RETRY_DELAYS", ())

    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        identity = "POI001" if layer == "population" else "road"
        try:
            if layer == "population":
                await boundary(factory)
                await citydata.run_citydata(
                    engine,
                    AsyncMock(return_value=CityDataDocument(document(), datetime.now(UTC))),
                    "test",
                    expected_code=identity,
                )
                failed = await citydata.run_citydata(
                    engine,
                    AsyncMock(side_effect=CollectionError("provider_http_error")),
                    "test",
                    expected_code=identity,
                )
            else:
                await seed_roads(factory, [identity])
                await topis.run_topis(
                    engine, AsyncMock(return_value=response(identity, 0, 0)), [identity]
                )
                failed = await topis.run_topis(
                    engine,
                    AsyncMock(side_effect=CollectionError("provider_http_error")),
                    [identity],
                )
            async with factory() as s:
                before = (await MapRepository(s).get_observations(layer, [identity]))[identity]
                assert before.collection_failed and before.value is not None
            for _ in range(21):
                async with factory.begin() as s:
                    repo = CollectionRepository(s)
                    run = await repo.start(source, "all")
                    await repo.finish(run, status="success")
            result = await publish_result(engine, {"status": "success"}, observations=(layer,))
            assert result["publication"]["status"] == "success"
            async with factory() as s:
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(CollectionRun)
                        .where(CollectionRun.source == source)
                    )
                    == 20
                )
                assert (
                    await s.scalar(
                        text("SELECT count(*) FROM collection_runs WHERE id=CAST(:id AS uuid)"),
                        {"id": failed["run_id"]},
                    )
                    == 0
                )
                after = (await MapRepository(s).get_observations(layer, [identity]))[identity]
                assert after == before
                model = MapPopulationObservation if layer == "population" else MapRoadObservation
                current = await s.get(model, identity)
                assert current.attempt_status == "failed"
                assert current.attempt_error_code == "provider_http_error"
                assert current.last_attempt_id is None
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_citydata_interrupt_keeps_committed_population_and_one_summary(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(citydata.asyncio, "sleep", AsyncMock())

    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        targets = {"POI001": "first", "POI002": "second"}

        async def fetch(code, name):
            if code == "POI002":
                raise asyncio.CancelledError()
            return CityDataDocument(document(code, name), datetime.now(UTC))

        try:
            await boundary(factory, targets)
            with pytest.raises(asyncio.CancelledError):
                await citydata.run_citydata_batch(engine, fetch, targets)
            async with factory() as s:
                runs = list(await s.scalars(select(CollectionRun)))
                assert len(runs) == 1
                assert runs[0].status == "partial" and runs[0].accepted == 1
                assert runs[0].error_code == "collection_interrupted"
                assert await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert await s.scalar(text("SELECT count(*) FROM parking_source_records")) == 0
            # Recovery reads committed DB values; it never needs an archived response.
            result = await recover_publication(engine, "citydata")
            assert result["layers"]["population"]["status"] == "success"
            async with factory() as s:
                assert (await s.get(MapPopulationObservation, "POI001")).max_people == 10
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_recovery_respects_active_collector_lease_and_missing_db_references(normalized_database):
    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as s:
                repo = CollectionRepository(s)
                run = await repo.start("seoul_citydata", "all")
                identity = run.id
                references = ReferenceRepository(s)
                assert not await references.has_prerequisite("citydata_places")
                assert not await references.has_prerequisite("sdot_locations")
            async with engine.connect() as lease:
                await lease.execute(text("SELECT pg_advisory_lock(hashtext('seoul_citydata'))"))
                await lease.commit()
                assert await recover_collection(engine, "seoul_citydata") == 0
                await lease.execute(text("SELECT pg_advisory_unlock(hashtext('seoul_citydata'))"))
                await lease.commit()
            assert await recover_collection(engine, "seoul_citydata") == 1
            async with factory() as s:
                closed = await s.get(CollectionRun, identity)
                assert closed.status == "failed" and closed.error_code == "collection_interrupted"
            await boundary(factory)
            async with factory() as s:
                assert await ReferenceRepository(s).has_prerequisite("citydata_places")
        finally:
            await engine.dispose()

    asyncio.run(check())
