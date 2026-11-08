"""Part 1 spatial readers in a disposable DB, never change the user's DB."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import read_session_factory
from app.models import DataSource, MapFeature, MapLayerVersion, ParkingFacility, PopulationArea
from app.repositories.area_queries import AreaQueryRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.schemas.location import Coordinates
from app.services.area_queries import AreaQueryService
from app.services.parking_nearby import ParkingNearbyService
from app.services.query_cursors import QueryCursorStore

pytestmark = pytest.mark.integration


def test_part1_radius_pages_name_filter_and_area_savepoint(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with async_sessionmaker(engine)() as session:
                session.add(
                    DataSource(source_code="part1_test", display_name="test", provider_name="test")
                )
                session.add_all(
                    [
                        MapLayerVersion(
                            layer=layer, geometry_version="test-v1", is_ready=True, coverage="test"
                        )
                        for layer in ("parking", "population")
                    ]
                )
                await session.flush()
                session.add(
                    PopulationArea(
                        area_code="P1TEST",
                        name="test observation area",
                        source="part1_test",
                        geometry=(
                            "SRID=4326;MULTIPOLYGON(((126.97 37.56,126.99 37.56,"
                            "126.99 37.58,126.97 37.58,126.97 37.56)))"
                        ),
                    )
                )
                keys = [uuid4(), uuid4()]
                for key, name, lon in zip(keys, ["alpha", "beta"], [126.978, 126.979], strict=True):
                    session.add(
                        ParkingFacility(
                            parking_id=key,
                            name=name,
                            location=f"SRID=4326;POINT({lon} 37.5665)",
                            identity_status="unverified",
                        )
                    )
                await session.flush()
                for key, name, lon in zip(keys, ["alpha", "beta"], [126.978, 126.979], strict=True):
                    session.add(
                        MapFeature(
                            layer="parking",
                            feature_id=str(key),
                            parking_id=key,
                            name=name,
                            identity_status="unverified",
                            geometry=f"SRID=4326;POINT({lon} 37.5665)",
                        )
                    )
                await session.commit()
            async with read_session_factory(engine)() as session:
                center = Coordinates(latitude=37.5665, longitude=126.978)
                repo = ParkingNearbyRepository(session)
                count, nearest = await repo.summarize_radius(center, 300)
                assert count == 2 and nearest == 0
                names = await repo.list_facilities(center, 2000, name="beta", limit=10)
                assert len(names) == 1 and names[0][0] == keys[1]
                provider = AsyncMock()
                provider.region.return_value = {
                    "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
                }
                service = ParkingNearbyService(repo, provider, cursors=QueryCursorStore())
                first = await service.search(center, 1, None)
                second = await service.search(center, 1, first.parking.next_cursor)
                assert first.parking.items[0].parking_id == keys[0]
                assert second.parking.items[0].parking_id == keys[1]
                assert (
                    second.parking.nearest_distance_meters == 0 and second.parking.total_count == 2
                )

                class FailedPopulation(AreaQueryRepository):
                    async def population(self, area_code):
                        await self.session.execute(text("SELECT 1 / 0"))

                area = await AreaQueryService(FailedPopulation(session)).overview("P1TEST")
                assert area["population"]["status"] == "error"
                assert area["parking"]["value"]["registered_facility_count"] == 2
                assert area["roads"]["status"] == "unavailable"
        finally:
            await engine.dispose()

    asyncio.run(run())
