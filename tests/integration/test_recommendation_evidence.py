"""DB0012 recommendation evidence; isolated database, no provider calls."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import create_engine
from app.models import (
    MapFeature,
    MapLayerVersion,
    MapRoadObservation,
    ParkingFacility,
    ParkingSourceRecord,
    RoadLink,
)
from app.repositories.collection import CollectionRepository
from app.repositories.recommendation import RecommendationEvidenceRepository
from app.schemas.recommendation import Place

pytestmark = pytest.mark.integration


def test_published_facility_distance_uuid_and_topis_freshness(normalized_database):
    async def run():
        engine = create_engine(Settings(_env_file=None, database_url=normalized_database))
        try:
            async with AsyncSession(engine) as session:
                for layer in ("parking", "roads"):
                    version = await session.get(MapLayerVersion, layer)
                    if version is None:
                        version = MapLayerVersion(layer=layer, coverage="test")
                        session.add(version)
                    version.is_ready, version.geometry_version = True, "test-v1"
                await CollectionRepository(session).ensure_source("recommendation_fixture")
                await session.flush()
                nearest_id = uuid4()
                for key, name, lat, active, published in (
                    (nearest_id, "near", 37.5001, True, True),
                    (uuid4(), "far", 37.504, True, True),
                    (uuid4(), "inactive", 37.5, False, True),
                    (uuid4(), "not published", 37.5, True, False),
                ):
                    point = func.ST_SetSRID(func.ST_MakePoint(127, lat), 4326)
                    session.add(
                        ParkingFacility(
                            parking_id=key,
                            name=name,
                            location=point,
                            identity_status="unverified",
                            is_active=active,
                        )
                    )
                    await session.flush()
                    if published:
                        session.add(
                            MapFeature(
                                layer="parking",
                                feature_id=str(key),
                                parking_id=key,
                                name=name,
                                geometry=point,
                                identity_status="unverified",
                                is_active=active,
                            )
                        )
                    if key == nearest_id:
                        session.add(
                            ParkingSourceRecord(
                                source_record_id=uuid4(),
                                parking_id=key,
                                source="recommendation_fixture",
                                name=name,
                                registered_capacity=100,
                                source_updated_at=None,
                                collected_at=datetime.now(UTC),
                                processor_version="test",
                                processed_at=datetime.now(UTC),
                            )
                        )
                road_id = "test-road"
                shape = func.ST_GeomFromText("MULTILINESTRING((126.999 37.5,127.001 37.5))", 4326)
                session.add(
                    RoadLink(link_id=road_id, source="recommendation_fixture", geometry=shape)
                )
                await session.flush()
                session.add(
                    MapFeature(
                        layer="roads", feature_id=road_id, road_link_id=road_id, geometry=shape
                    )
                )
                await session.flush()
                now = datetime.now(UTC)
                observation = MapRoadObservation(
                    link_id=road_id,
                    layer="roads",
                    source="recommendation_fixture",
                    has_observation=True,
                    speed_kmh=0,
                    travel_seconds=25,
                    observed_at=None,
                    received_at=now,
                    processed_at=now,
                    processor_version="test",
                    collection_failed=False,
                )
                session.add(observation)
                await session.flush()
                repo = RecommendationEvidenceRepository(session)
                await repo.load()
                place = Place(id="1", name="식당", x=127, y=37.5)
                traffic, parking = await repo.for_place(place)
                assert parking.id == str(nearest_id)
                assert 10 < parking.distance_m < 12
                assert parking.facility.parking_id == nearest_id
                assert parking.facility.records[0].registered.value.registered_capacity == 100
                assert parking.facility.availability.status == "unsupported"
                assert parking.source_time is None
                assert traffic.status == "확인 불가"  # no inferred congestion grade
                assert traffic.roads[0].traffic.value["speed_kmh"] == 0
                assert traffic.roads[0].traffic.freshness == "unknown"
                observation.received_at = now - timedelta(minutes=31)
                await session.flush()
                traffic, _ = await repo.for_place(place)
                assert traffic.roads[0].traffic.value is None
                assert traffic.roads[0].traffic.reason_code == "STALE_DATA"
                version = await session.get(MapLayerVersion, "parking")
                version.is_ready = False
                await session.flush()
                await repo.load()
                assert (await repo.for_place(place))[1] is None
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())
