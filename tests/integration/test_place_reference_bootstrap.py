"""Local bootstrap publishes empty databases and preserves existing reference sets."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import CollectionRun, MapFeature, MapLayerVersion, PopulationArea
from app.repositories.map import MapRepository
from app.schemas.map import MapQuery
from app.services.map import MapService
from app.services.map_publication import MapPublicationService, PublicationNotReady
from jobs import import_place_boundaries as job

pytestmark = pytest.mark.integration
QUERY = MapQuery(bbox=(126.7, 37.3, 127.3, 37.8), level=6)


@pytest.fixture
def database(normalized_database, monkeypatch):
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, database_url=normalized_database)
    )
    return normalized_database


def without_files(tmp_path):
    return job.arguments(
        [
            "--if-empty",
            "--areas-file",
            str(tmp_path / "absent.xlsx"),
            "--boundaries-file",
            str(tmp_path / "absent.zip"),
        ]
    )


async def canonical(session):
    return (
        await session.execute(select(PopulationArea.__table__).order_by(PopulationArea.area_code))
    ).all()


async def published(session):
    version = await session.get(MapLayerVersion, "population")
    features = (
        await session.execute(
            select(MapFeature.__table__)
            .where(MapFeature.layer == "population")
            .order_by(MapFeature.feature_id)
        )
    ).all()
    return version.geometry_version, version.updated_at, features


def test_first_start_publishes_and_restart_does_not_reimport(database, tmp_path):
    async def run():
        engine = create_async_engine(database)
        factory = async_sessionmaker(engine)
        try:
            assert await job.execute(job.arguments(["--if-empty"])) == 0
            async with factory() as session:
                reference = await canonical(session)
                publication = await published(session)
                imports = await session.scalar(select(func.count()).select_from(CollectionRun))
                response = await MapService(MapRepository(session)).geometry("population", QUERY)
                assert len(reference) == len(response.feature_collection.features) == 121
            # A restart must work even without the original input files.
            assert await job.execute(without_files(tmp_path)) == 0
            async with factory() as session:
                assert await canonical(session) == reference
                assert await published(session) == publication
                assert (
                    await session.scalar(select(func.count()).select_from(CollectionRun)) == imports
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("unavailable"), PublicationNotReady("not_ready")])
def test_failed_publication_blocks_start_then_recovers_stored_reference(
    database, tmp_path, monkeypatch, error
):
    async def run():
        engine = create_async_engine(database)
        factory = async_sessionmaker(engine)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(MapPublicationService, "publish", AsyncMock(side_effect=error))
                # Storage commits, but Compose must not start readers without a map.
                assert await job.execute(job.arguments(["--if-empty"])) == 2
            async with factory.begin() as session:
                assert await session.get(MapLayerVersion, "population") is None
                stored = await canonical(session)
                assert len(stored) == 121
                # A newer reference may differ from the checkout's bundled files.
                await session.execute(
                    update(PopulationArea).values(source_version="newer-reference")
                )
                await session.execute(
                    update(PopulationArea)
                    .where(PopulationArea.area_code == stored[0].area_code)
                    .values(is_active=False)
                )
            async with factory() as session:
                reference = await canonical(session)
            assert await job.execute(without_files(tmp_path)) == 0
            async with factory() as session:
                assert await canonical(session) == reference
                response = await MapService(MapRepository(session)).geometry("population", QUERY)
                assert len(response.feature_collection.features) == 120
                assert stored[0].area_code not in {
                    feature.id for feature in response.feature_collection.features
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inactive_reference_is_not_replaced_by_bootstrap(database, tmp_path):
    async def run():
        engine = create_async_engine(database)
        factory = async_sessionmaker(engine)
        try:
            assert await job.execute(job.arguments(["--if-empty"])) == 0
            async with factory.begin() as session:
                await session.execute(update(PopulationArea).values(is_active=False))
            async with factory() as session:
                reference = await canonical(session)
            assert await job.execute(without_files(tmp_path)) == 2
            async with factory() as session:
                assert await canonical(session) == reference
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_input_fails_without_partial_reference(database, tmp_path):
    async def run():
        with pytest.raises(FileNotFoundError):
            await job.execute(without_files(tmp_path))
        engine = create_async_engine(database)
        try:
            async with async_sessionmaker(engine)() as session:
                assert await canonical(session) == []
                assert await session.get(MapLayerVersion, "population") is None
        finally:
            await engine.dispose()

    asyncio.run(run())
