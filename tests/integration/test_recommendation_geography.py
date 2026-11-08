"""Real PostGIS facts -> recommendation evidence -> historical snapshot; no live providers."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.clients.area_extras import SeoulAreaExtras
from app.db.session import read_session_factory, write_session_factory
from app.models import (
    AdministrativeArea,
    Base,
    MapFeature,
    MapLayerVersion,
    ParkingFacility,
    RoadAreaMapping,
    RoadLink,
    RoadLinkMapping,
)
from app.repositories.normalization import NormalizedWriter
from app.repositories.recommendation_context import RecommendationContextRepository
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.repositories.reference_import import ReferenceImportWriter
from app.schemas.location import Coordinates
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_geography import SOURCE, RecommendationGeographyService
from tests.fixtures.recommendation_context import seed_conditions
from tests.unit.recommendation.test_geography import CENTER, area, sections
from tests.unit.recommendation.test_progress import candidate, request, setup

pytestmark = pytest.mark.integration


async def seed(session):
    now = datetime.now(UTC)
    common = dict(processor_version="test", processed_at=now, source_version="v1")
    for code in ("sgis_areas", "topis_geometry", "topis_standard_mapping"):
        await session.run_sync(lambda s, code=code: NormalizedWriter(s, Base.metadata).source(code))
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [[ring + [ring[0]] for ring in polygon] for polygon in area().polygons],
    }
    await session.run_sync(
        lambda s: ReferenceImportWriter(s).places(
            [
                {"id": "POI001", "properties": {"name": "명소"}, "geometry": geometry},
            ],
            "v1",
            now,
        )
    )
    for code, active, sigungu in [
        ("dong", True, "11010"),
        ("inactive", False, "11010"),
        ("not-seoul", True, "12010"),
    ]:
        session.add(
            AdministrativeArea(
                dong_code=code,
                name=code,
                sigungu_code=sigungu,
                code_system="sgis",
                source="sgis_areas",
                is_active=active,
                boundary_version="boundary-v1",
                road_mapping_version="roads-v1",
                geometry=func.ST_GeomFromGeoJSON(json.dumps(geometry)),
                **common,
            )
        )
    session.add_all(
        [
            MapLayerVersion(
                layer=layer, geometry_version=f"{layer}-v1", is_ready=True, coverage="test"
            )
            for layer in ("roads", "parking")
        ]
    )
    for i, (lon, lat, active, published) in enumerate(
        [
            (126.98, 37.57, True, True),
            (127.01, 37.60, True, True),
            (126.972, 37.562, True, True),
            (126.98, 37.57, False, True),
            (126.98, 37.57, True, False),
        ]
    ):
        key = uuid4()
        point = f"SRID=4326;POINT({lon} {lat})"
        session.add(ParkingFacility(parking_id=key, name=str(i), location=point, is_active=active))
        await session.flush()
        session.add(
            MapFeature(
                layer="parking",
                feature_id=str(key),
                parking_id=key,
                geometry=point,
                name=str(i),
                identity_status="unverified",
                is_active=published,
            )
        )
    for service_id, standard_id, lon in [
        ("service-a", "1000001401", 126.98),
        ("service-b", "1000000301", 126.985),
    ]:
        line = f"SRID=4326;MULTILINESTRING(({lon} 37.569,{lon} 37.571))"
        session.add(RoadLink(link_id=service_id, source="topis_geometry", geometry=line, **common))
        await session.flush()
        session.add_all(
            [
                MapFeature(
                    layer="roads", feature_id=service_id, road_link_id=service_id, geometry=line
                ),
                RoadLinkMapping(
                    service_link_id=service_id,
                    standard_link_id=standard_id,
                    sequence=1,
                    sequence_status="known",
                    source="topis_standard_mapping",
                    **common,
                ),
                RoadAreaMapping(
                    link_id=service_id,
                    dong_code="dong",
                    boundary_version="boundary-v1",
                    road_geometry_version="roads-v1",
                    processor_version="test",
                    mapped_at=now,
                ),
            ]
        )
    return now


def test_spatial_scopes_source_ids_and_snapshot_round_trip(normalized_database, no_runtime_files):
    async def run():
        engine = create_async_engine(normalized_database)
        calls = []
        try:
            async with write_session_factory(engine).begin() as session:
                observed = await seed(session)

            def respond(req):
                calls.append(req)
                return httpx.Response(
                    200,
                    json={
                        "RESULT": {"CODE": "INFO-000"},
                        "CITYDATA": {
                            "AREA_CD": "POI001",
                            "AREA_NM": "명소",
                            **sections(
                                ("1000001401", "서행"),
                                ("1000000301", "서행"),
                                at=observed.isoformat(),
                            ),
                        },
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                async with read_session_factory(engine)() as session:
                    repo = RecommendationGeographyRepository(session)
                    geo = RecommendationGeographyService(repo, SeoulAreaExtras(http, "test-key"))
                    areas = await geo.areas(CENTER)
                    assert [a.area_id for a in areas] == ["dong"]
                    assert areas[0].contains(areas[0].center)
                    assert len(areas[0].polygons) == 2
                    assert areas[0].registered_parking_count == 2
                    assert areas[0].congestion == "서행"
                    assert await repo.area_roads() == {"dong": {"1000001401", "1000000301"}}
                    assert await repo.nearby_roads(CENTER, 100) == {"1000001401"}
                    assert (await geo.traffic(CENTER)).grade == "서행"
                    outside = Coordinates(longitude=127.03, latitude=37.62)
                    assert await repo.hotspots(outside, covering=True, limit=3) == []
                    assert (await geo.traffic(outside)).grade is None
                    service, source, recommender = setup()
                    source.search.side_effect = None
                    source.search.return_value = ([candidate("kakao:one")], True)
                    recommender.area_provider = recommender.traffic_provider = geo
                    req = request()
                    outcome = await service.execute(req)
                    assert outcome.failure is None
                    data = outcome.response
                    evidence = data.result["cards"][0]["reason"]["evidence"][0]["information"]
                    assert evidence["source"] == SOURCE and evidence["value"] == {"grade": "서행"}
                    assert len(calls) == 1
                    assert (
                        await session.execute(text("SHOW transaction_read_only"))
                    ).scalar() == "on"
                    assert not session.new and not session.dirty
            async with write_session_factory(engine).begin() as session:
                await seed_conditions(session, data)
                stored = await RecommendationContextService(
                    RecommendationContextRepository(session),
                ).register(req.request_id, data)
            async with read_session_factory(engine)() as session:
                snapshot = await RecommendationContextService(
                    RecommendationContextRepository(session),
                ).resolve(stored.result["context_token"])
                assert snapshot.search_areas[0].registered_parking_count == 2
                assert snapshot.search_areas[0].road_observed_at == observed
                traffic = next(item for item in snapshot.evidence if item.type == "traffic")
                assert traffic.source == SOURCE and traffic.observed_at == observed
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_mappings_missing_publication_and_read_failure_are_unknown(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with write_session_factory(engine).begin() as session:
                now = await seed(session)

            async def change(statement):
                async with write_session_factory(engine).begin() as session:
                    await session.execute(statement)

            async def facts():
                async with read_session_factory(engine)() as session:
                    repo = RecommendationGeographyRepository(session)
                    return await repo.area_roads(), await repo.nearby_roads(CENTER, 100)

            await change(
                update(RoadAreaMapping)
                .where(RoadAreaMapping.link_id == "service-b")
                .values(boundary_version="old")
            )
            assert (await facts())[0] == {"dong": {"1000001401", None}}
            await change(
                update(RoadLinkMapping)
                .where(RoadLinkMapping.service_link_id == "service-a")
                .values(processed_at=now - timedelta(seconds=1))
            )
            assert (await facts())[1] == {None}
            await change(update(RoadLinkMapping).values(processed_at=now))
            await change(
                update(MapFeature).where(MapFeature.layer == "roads").values(is_active=False)
            )
            assert (await facts())[1] == {None}
            await change(update(MapLayerVersion).values(is_ready=False))
            assert await facts() == ({}, set())
            async with read_session_factory(engine)() as session:
                assert await RecommendationGeographyRepository(session).parking_counts() is None
            await change(update(MapLayerVersion).values(is_ready=True))
            await change(update(ParkingFacility).values(is_active=False))
            async with read_session_factory(engine)() as session:
                counts, _ = await RecommendationGeographyRepository(session).parking_counts()
                assert counts == {}  # Available and genuinely zero, distinct from unavailable.

                class BrokenRead(RecommendationGeographyRepository):
                    async def ready_version(self, layer):
                        await self.session.execute(text("SELECT 1 / 0"))

                from unittest.mock import AsyncMock

                geo = RecommendationGeographyService(BrokenRead(session), AsyncMock())
                assert (await geo.traffic(CENTER)).reason == "DATA_UNAVAILABLE"
                assert (
                    await session.scalar(select(func.count()).select_from(AdministrativeArea)) == 3
                )
                assert len(await geo.areas(CENTER)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
