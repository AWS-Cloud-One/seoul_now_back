"""Readers see a complete committed snapshot without waiting for in-progress updates."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import get_session
from app.core.config import Settings
from app.db.session import read_session_factory
from app.main import create_app
from app.models import (
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
    ParkingFacility,
    ParkingSourceRecord,
)
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.modules.data_pipeline.normalizers.storage_fields import population
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.map import MapRepository
from app.repositories.parking_detail import ParkingDetailRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.population import PopulationWriter
from app.services.map_publication import MapPublicationService
from tests.fixtures.citydata import boundary, document
from tests.fixtures.topis import response, seed_roads, write

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_runtime_files")]
BBOX = "126.9,37.4,127.2,37.7"


async def seed_map(factory, layer):
    now = datetime.now(UTC)
    if layer == "roads":
        await seed_roads(factory, ["road"])
        await write(factory, "road", response("road", 10, 10), now)
    else:
        await boundary(factory)
        body = document()["CITYDATA"]
        body["LIVE_PPLTN_STTS"][0]["PPLTN_TIME"] = now.astimezone(ZoneInfo("Asia/Seoul")).strftime(
            "%Y-%m-%d %H:%M"
        )
        values = population(body, received_at=now)
        values.pop("quality")
        async with factory.begin() as s:
            await s.run_sync(lambda sync: PopulationWriter(sync).observation("POI001", values, now))
    async with factory.begin() as s:
        await MapPublicationService(s).publish(layer)


@pytest.mark.parametrize("layer", ["roads", "population"])
@pytest.mark.parametrize("endpoint", ["geometry", "status"])
def test_http_response_keeps_old_version_features_and_values_across_writer_commit(
    normalized_database, monkeypatch, layer, endpoint
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        app = create_app(Settings(_env_file=None, database_url=normalized_database))
        try:
            await seed_map(factory, layer)
            async with factory() as s:
                previous = (await s.get(MapLayerVersion, layer)).geometry_version
            writer = factory()
            await writer.begin()
            try:
                await writer.execute(
                    update(MapLayerVersion)
                    .where(MapLayerVersion.layer == layer)
                    .values(geometry_version="after")
                )
                await writer.execute(
                    update(MapFeature)
                    .where(MapFeature.layer == layer)
                    .values(name="after", geometry=func.ST_Translate(MapFeature.geometry, 0.001, 0))
                )
                observation = MapRoadObservation if layer == "roads" else MapPopulationObservation
                values = (
                    {"speed_kmh": 20, "travel_seconds": 20}
                    if layer == "roads"
                    else {"min_people": 20, "max_people": 30}
                )
                await writer.execute(update(observation).values(collection_failed=True, **values))
                original = MapRepository.select_features
                committed = False

                async def interleave(repository, *args, **kwargs):
                    nonlocal committed
                    # The API has already read the old version without waiting for writer locks.
                    if not committed:
                        await writer.commit()
                        committed = True
                    return await original(repository, *args, **kwargs)

                monkeypatch.setattr(MapRepository, "select_features", interleave)
                params = {"bbox": BBOX, "level": 6}
                if endpoint == "status":
                    params["geometry_version"] = previous
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as http:
                        response = await asyncio.wait_for(
                            http.get(f"/api/v1/map/layers/{layer}/{endpoint}", params=params),
                            timeout=3,
                        )
                        assert response.status_code == 200, response.text
                        data = response.json()["data"]
                        assert data["geometry_version"] == previous
                        if endpoint == "geometry":
                            feature = data["feature_collection"]["features"][0]
                            assert feature["properties"].get("name") != "after"
                            assert "127.001" not in str(feature["geometry"])
                        else:
                            field = "traffic" if layer == "roads" else "population"
                            assert data["items"][0][field]["reason_code"] != "COLLECTION_FAILED"
                            value = data["items"][0][field]["value"]
                            if layer == "roads":
                                assert value["speed_kmh"] == value["travel_seconds"] == 10
                            else:
                                assert value["min_people"] == 0 and value["max_people"] == 10
                        # A new request sees the newly committed complete publication.
                        if endpoint == "status":
                            params["geometry_version"] = "after"
                        response = await http.get(
                            f"/api/v1/map/layers/{layer}/{endpoint}", params=params
                        )
                        assert response.status_code == 200
                        data = response.json()["data"]
                        assert data["geometry_version"] == "after"
                        if endpoint == "geometry":
                            assert (
                                data["feature_collection"]["features"][0]["properties"]["name"]
                                == "after"
                            )
                        else:
                            assert data["items"][0][field]["reason_code"] == "COLLECTION_FAILED"
                            for key, value in values.items():
                                assert data["items"][0][field]["value"][key] == value
            finally:
                await writer.close()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("commit", [True, False])
def test_parking_readers_do_not_block_and_never_mix_registration_versions(
    normalized_database, commit
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        readers = read_session_factory(engine)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(session_factory=readers))
        )

        async def fetch():
            return [
                {
                    "PKLT_CD": "1",
                    "PKLT_NM": "before",
                    "ADDR": "before",
                    "LAT": 37.5,
                    "LOT": 127,
                    "TPKCT": "10",
                }
            ]

        try:
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 1
            async with factory() as s:
                identity = await s.scalar(select(ParkingFacility.parking_id))
            async with factory() as writer:
                await writer.begin()
                await writer.execute(update(ParkingFacility).values(name="after"))
                await writer.execute(update(ParkingSourceRecord).values(address="after"))
                await writer.execute(
                    update(MapLayerVersion)
                    .where(MapLayerVersion.layer == "parking")
                    .values(geometry_version="after")
                )
                session_context = get_session(request)
                reader = await anext(session_context)
                try:
                    detail = ParkingDetailRepository(reader)
                    nearby = ParkingNearbyRepository(reader)
                    facility = await asyncio.wait_for(detail.get_facility(identity), timeout=2)
                    version = await asyncio.wait_for(nearby.get_version(), timeout=2)
                    assert facility.name == "before" and version.geometry_version != "after"
                    await (writer.commit() if commit else writer.rollback())
                    assert (await detail.list_records(identity))[0].address == "before"
                    assert (await nearby.list_records([identity]))[identity][0].address == "before"
                finally:
                    await session_context.aclose()
            async with readers() as s:
                assert (await ParkingDetailRepository(s).list_records(identity))[0].address == (
                    "after" if commit else "before"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_read_only_snapshot_cannot_write_and_pool_reuse_does_not_change_worker_mode(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database, pool_size=1, max_overflow=0)
        readers = read_session_factory(engine)
        writers = async_sessionmaker(engine)
        try:
            async with readers() as s:
                assert await s.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                assert await s.scalar(text("SHOW transaction_read_only")) == "on"
                with pytest.raises(DBAPIError) as error:
                    await s.execute(text("UPDATE map_layer_versions SET coverage='forbidden'"))
                assert error.value.orig.sqlstate == "25006"
            async with writers.begin() as s:
                assert await s.scalar(text("SHOW transaction_isolation")) == "read committed"
                assert await s.scalar(text("SHOW transaction_read_only")) == "off"
                await s.execute(text("UPDATE map_layer_versions SET coverage='allowed'"))
        finally:
            await engine.dispose()

    asyncio.run(run())
