"""Collection -> canonical rows -> atomic publication -> existing spatial reads."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import (
    CollectionRun,
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.pipelines import citydata, topis
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.collection import CollectionRepository
from app.repositories.map import MapRepository
from app.schemas.map import MapQuery
from app.services.map import MapService
from app.services.map_publication import MapPublicationService
from tests.fixtures.road_reference import save_fixture_snapshot

pytestmark = pytest.mark.integration
LINK = "1000000000"
QUERY = MapQuery(bbox=(126.9, 37.4, 127.2, 37.7), level=6)


async def reference(factory, *, end=127.01):
    async with factory.begin() as s:
        result = await save_fixture_snapshot(
            s,
            "topis_geometry:test",
            {
                "features": [
                    {
                        "id": LINK,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [end, 37.5]],
                        },
                    }
                ]
            },
        )
        assert result.status == "success"


async def publish(engine, layer):
    return await publish_result(engine, {"status": "success"}, geometry=(layer,))


async def state(factory, layer="roads"):
    async with factory() as s:
        version = await s.get(MapLayerVersion, layer)
        features = (
            await s.execute(
                select(
                    MapFeature.feature_id,
                    MapFeature.published_at,
                    func.ST_AsGeoJSON(MapFeature.geometry),
                )
                .where(MapFeature.layer == layer)
                .order_by(MapFeature.feature_id)
            )
        ).all()
        model = MapRoadObservation if layer == "roads" else MapPopulationObservation
        observations = (await s.execute(select(model.__table__).order_by(model.published_at))).all()
        return version.geometry_version, version.updated_at, features, observations


def test_traffic_collection_publishes_without_rebuilding_and_preserves_failure_states(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await reference(factory)
            assert (await publish(engine, "roads"))["publication"]["status"] == "success"
            original = await state(factory)

            async def fetch(link):
                return {"rows": [{"link_id": link, "prcs_spd": "0", "prcs_trv_time": "0"}]}

            result = await topis.run_topis(engine, fetch, [LINK])
            assert result["publication"]["layers"]["roads"] == {
                "status": "success",
                "mode": "observations",
                "features": 1,
                "geometry_changed": False,
                "observations_changed": 1,
            }
            good = await state(factory)
            assert good[0] == original[0] and good[2] == original[2]
            async with factory() as s:
                service = MapService(MapRepository(s))
                response = await service.status("roads", QUERY, good[0])
                assert response.items[0].traffic.value.speed_kmh == 0
                assert response.items[0].traffic.observed_at is None
                assert response.items[0].traffic.collected_at is not None
                canonical = await s.get(RoadTrafficLatest, ("topis_traffic", LINK))
                payload = canonical.payload_id
            # An unfinished newer attempt cannot replace the last completed attempt.
            async with factory.begin() as s:
                await CollectionRepository(s).start("topis_traffic", "link:" + LINK)
            repeated = await publish(engine, "roads")
            assert repeated["publication"]["layers"]["roads"]["observations_changed"] == 0
            assert await state(factory) == good
            for error, has, failed in [
                ("provider_http_error", True, True),
                ("provider_no_observation", False, False),
            ]:

                async def unavailable(_link, error=error):
                    raise CollectionError(error)

                result = await topis.run_topis(engine, unavailable, [LINK])
                assert result["publication"]["status"] == "success"
                async with factory() as s:
                    row = await s.get(MapRoadObservation, LINK)
                    assert row.has_observation is has and row.collection_failed is failed
                    assert (
                        await s.get(RoadTrafficLatest, ("topis_traffic", LINK))
                    ).payload_id == payload
                    if has:
                        assert row.payload_id == payload and row.speed_kmh == 0
                    else:
                        assert row.payload_id is None and row.speed_kmh is None
                assert (await state(factory))[2] == original[2]
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_publication_failure_rolls_back_only_publication_and_manual_retry_is_idempotent(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await reference(factory)
            await publish(engine, "roads")
            previous = await state(factory)
            original = MapPublicationService._finish

            async def fail_after_writes(self, version):
                await original(self, version)
                raise RuntimeError("private database details")

            monkeypatch.setattr(MapPublicationService, "_finish", fail_after_writes)
            result = await topis.run_topis(
                engine,
                AsyncMock(
                    return_value={
                        "rows": [{"link_id": LINK, "prcs_spd": "12", "prcs_trv_time": "30"}]
                    }
                ),
                [LINK],
            )
            assert result["status"] == "success"
            assert result["publication"]["status"] == "failed"
            assert "private" not in str(result)
            assert await state(factory) == previous
            async with factory() as s:
                assert (await s.get(RoadTrafficLatest, ("topis_traffic", LINK))).speed_kmh == 12
                runs = list(
                    await s.scalars(
                        select(CollectionRun).where(CollectionRun.target_key == "network")
                    )
                )
                assert len(runs) == 1 and runs[0].status == "success"
            monkeypatch.setattr(MapPublicationService, "_finish", original)
            assert (await publish(engine, "roads"))["publication"]["status"] == "success"
            retried = await state(factory)
            assert retried[2] == previous[2]
            await publish(engine, "roads")
            assert await state(factory) == retried
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_reference_change_is_recovered_atomically_by_next_collection(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await reference(factory)
            missing = await publish_result(engine, {"status": "success"}, observations=("roads",))
            assert missing["publication"]["status"] == "success"
            before = await state(factory)
            await reference(factory, end=127.02)
            fetch = AsyncMock(
                return_value={"rows": [{"link_id": LINK, "prcs_spd": "8", "prcs_trv_time": "50"}]}
            )
            original = MapPublicationService._finish

            async def fail(self, version):
                await original(self, version)
                raise RuntimeError("failure after replacing geometry and observations")

            monkeypatch.setattr(MapPublicationService, "_finish", fail)
            result = await topis.run_topis(engine, fetch, [LINK])
            assert result["status"] == "success" and result["publication"]["status"] == "failed"
            assert await state(factory) == before
            monkeypatch.setattr(MapPublicationService, "_finish", original)
            result = await topis.run_topis(engine, fetch, [LINK])
            assert result["publication"]["status"] == "success"
            assert result["publication"]["layers"]["roads"]["mode"] == "geometry"
            after = await state(factory)
            assert after[0] != before[0] and after[2] != before[2]
            async with factory() as s:
                response = await MapService(MapRepository(s)).status("roads", QUERY, after[0])
                assert response.items[0].traffic.value.speed_kmh == 8
                assert await s.scalar(select(func.count()).select_from(MapFeature)) == 1
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_citydata_batch_publishes_population_and_parking_then_exposes_failed_attempt(
    normalized_database, monkeypatch
):
    async def check():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "citydata_place_boundaries",
                    {
                        "features": [
                            {
                                "id": "POI001",
                                "properties": {"name": "test"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [[127, 37.5], [127.1, 37.5], [127.1, 37.6], [127, 37.5]]
                                    ],
                                },
                            }
                        ]
                    },
                )
            await publish(engine, "population")
            original = await state(factory, "population")
            now = datetime.now(UTC)
            local_time = (now + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
            body = {
                "AREA_CD": "POI001",
                "AREA_NM": "test",
                "LIVE_PPLTN_STTS": [
                    {
                        "AREA_CD": "POI001",
                        "AREA_NM": "test",
                        "AREA_PPLTN_MIN": "0",
                        "AREA_PPLTN_MAX": "10",
                        "AREA_CONGEST_LVL": "\uc5ec\uc720",
                        "REPLACE_YN": "N",
                        "PPLTN_TIME": local_time,
                    }
                ],
                "PRK_STTS": [
                    {
                        "PRK_CD": "1",
                        "PRK_NM": "parking",
                        "LAT": "37.51",
                        "LNG": "127.01",
                        "CPCTY": "10",
                        "CUR_PRK_YN": "Y",
                        "CUR_PRK_CNT": "0",
                        "CUR_PRK_TIME": local_time,
                    }
                ],
                "ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": []},
            }

            async def fetch(code, name):
                return CityDataDocument(body, now, require_envelope=False)

            calls = []
            refresh = MapPublicationService.refresh_observations

            async def track(self, layer):
                calls.append(layer)
                return await refresh(self, layer)

            monkeypatch.setattr(MapPublicationService, "refresh_observations", track)
            result = await citydata.run_citydata_batch(engine, fetch, {"POI001": "test"})
            assert result["publication"]["status"] == "success", result
            assert set(result["publication"]["layers"]) == {"population", "parking"}
            assert calls == ["population"]
            current = await state(factory, "population")
            assert current[0] == original[0] and current[2] == original[2]
            async with factory() as s:
                service = MapService(MapRepository(s))
                response = await service.status("population", QUERY, current[0])
                assert response.items[0].population.value.min_people == 0
                assert response.items[0].population.collected_at == now
                parking = await service.geometry("parking", QUERY)
                assert len(parking.feature_collection.features) == 1

            missing = {key: value for key, value in body.items() if key != "LIVE_PPLTN_STTS"}
            result = await citydata.run_citydata(
                engine,
                AsyncMock(
                    return_value=CityDataDocument(
                        missing, datetime.now(UTC), require_envelope=False
                    )
                ),
                "test",
                expected_code="POI001",
            )
            assert result["status"] == "partial" and result["publication"]["status"] == "success"
            async with factory() as s:
                response = await MapService(MapRepository(s)).status(
                    "population", QUERY, current[0]
                )
                assert response.items[0].population.reason_code == "COLLECTION_FAILED"
                assert response.items[0].population.value.min_people == 0
                assert response.items[0].population.collected_at == now

            async def failed():
                raise CollectionError("provider_http_error")

            result = await citydata.run_citydata(engine, failed, "test", expected_code="POI001")
            assert result["status"] == "failed" and result["publication"]["status"] == "success"
            async with factory() as s:
                response = await MapService(MapRepository(s)).status(
                    "population", QUERY, current[0]
                )
                assert response.items[0].population.value.min_people == 0
                assert response.items[0].population.reason_code == "COLLECTION_FAILED"
                assert response.items[0].population.collected_at == now
        finally:
            await engine.dispose()

    asyncio.run(check())
