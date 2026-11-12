"""Road name batches preserve geometry, survive reimport and reach the map response."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import Base, MapLayerVersion, RoadLink
from app.modules.data_pipeline.pipelines.road_names import run_road_names
from app.repositories.map import MapRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.road_reference import replace_road_reference
from app.schemas.map import MapQuery
from app.services.map import MapService
from tests.fixtures.topis import seed_roads

pytestmark = pytest.mark.integration
LINKS = ["1000000000", "1000000001"]


async def geometry_state(session):
    return (
        await session.execute(
            select(
                RoadLink.link_id,
                RoadLink.source,
                RoadLink.source_version,
                RoadLink.processed_at,
                func.ST_AsGeoJSON(RoadLink.geometry),
            ).order_by(RoadLink.link_id)
        )
    ).all()


def test_names_publish_without_changing_geometry_and_survive_geometry_reimport(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS)
            async with factory() as session:
                before = await geometry_state(session)
            fetch = AsyncMock(side_effect=lambda link: {"link_id": link, "name": "테스트도로"})
            result = await run_road_names(engine, fetch, links=LINKS)
            assert result["status"] == "success" and result["updated"] == 2
            async with factory() as session:
                assert await geometry_state(session) == before
                response = await MapService(MapRepository(session)).geometry(
                    "roads", MapQuery(bbox=(126.9, 37.4, 127.2, 37.7), level=6)
                )
                assert {f.properties.name for f in response.feature_collection.features} == {
                    "테스트도로"
                }
                version = (await session.get(MapLayerVersion, "roads")).geometry_version
            again = await run_road_names(engine, fetch, links=LINKS)
            assert again["updated"] == 0
            assert not again["publication"]["layers"]["roads"]["geometry_changed"]
            async with factory() as session:
                assert (await session.get(MapLayerVersion, "roads")).geometry_version == version
            # Coordinate replacement has no name field; the enriched name must survive.
            candidate = {
                "reference": {
                    "inputs": {
                        "vertices": {"version": "new", "reference_date": None},
                        "mapping": {"version": "new", "reference_date": None},
                    }
                },
                "features": [
                    {
                        "id": link,
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [127.02, 37.5]],
                        },
                    }
                    for link in LINKS
                ],
            }
            async with factory.begin() as session:
                await session.run_sync(
                    lambda sync: replace_road_reference(
                        NormalizedWriter(sync, Base.metadata), candidate, datetime.now(UTC)
                    )
                )
            async with factory() as session:
                roads = list(await session.scalars(select(RoadLink)))
                assert all(r.name == "테스트도로" and r.source_version == "new" for r in roads)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_lookup_preserves_name_and_batch_can_continue(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS)
            async with factory.begin() as session:
                await session.execute(
                    update(RoadLink).where(RoadLink.link_id == LINKS[0]).values(name="기존도로")
                )
            fetch = AsyncMock(side_effect=CollectionError("provider_auth_error"))
            result = await run_road_names(engine, fetch, refresh=True)
            assert result["status"] == "failed" and result["updated"] == 0
            assert result["has_more"] and result["next_after"] == LINKS[0]
            fetch.assert_awaited_once_with(LINKS[0])
            async with factory() as session:
                assert (await session.get(RoadLink, LINKS[0])).name == "기존도로"
                assert (await session.get(RoadLink, LINKS[1])).name is None
            fetch = AsyncMock(return_value={"link_id": LINKS[1], "name": "다음도로"})
            result = await run_road_names(engine, fetch, after=LINKS[0], limit=1)
            assert result["status"] == "success" and result["updated"] == 1
            assert not result["has_more"]
            fetch.assert_awaited_once_with(LINKS[1])
        finally:
            await engine.dispose()

    asyncio.run(run())
