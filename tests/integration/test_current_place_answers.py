import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import ServiceError
from app.db.session import read_session_factory, write_session_factory
from app.models import Base, MapFeature, MapLayerVersion, MapPopulationObservation, PopulationArea
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.types import RouteEstimate
from app.repositories.area_queries import AreaQueryRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.reference_import import ReferenceImportWriter
from app.schemas.location import Coordinates
from app.services.area_queries import AreaQueryService
from tests.fixtures.state_inspection import trip_state
from tests.integration.test_place_details_api_storage import parking_rows, travel_request
from tests.integration.test_recommendation_chat_api_storage import (
    C01,
    R01,
    body,
    chat,
    counts,
    following,
    post,
    runtime,
    target,
)
from tests.unit.recommendation.test_geography import CENTER, area
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


async def seed_population(session):
    now = datetime.now(UTC)
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [[ring + [ring[0]] for ring in polygon] for polygon in area().polygons],
    }
    await session.run_sync(
        lambda s: ReferenceImportWriter(s).places(
            [
                {"id": f"POI{i:03}", "properties": {"name": f"관측 구역 {i}"}, "geometry": geometry}
                for i in range(1, 6)
            ],
            "boundary-v1",
            now,
        )
    )
    await session.run_sync(lambda s: NormalizedWriter(s, Base.metadata).source("seoul_citydata"))
    session.add(
        MapLayerVersion(
            layer="population", geometry_version="published-v1", is_ready=True, coverage="test"
        )
    )
    await session.flush()
    for i in range(1, 5):
        code = f"POI{i:03}"
        shape = (
            func.ST_GeomFromGeoJSON(json.dumps(geometry))
            if i != 3
            else (
                "SRID=4326;MULTIPOLYGON((("
                "127.1 37.5,127.11 37.5,127.11 37.51,127.1 37.51,127.1 37.5)))"
            )
        )
        session.add(
            MapFeature(
                layer="population",
                feature_id=code,
                population_area_code=code,
                name=f"관측 구역 {i}",
                geometry=shape,
            )
        )
        await session.flush()
        session.add(
            MapPopulationObservation(
                area_code=code,
                source="seoul_citydata",
                has_observation=True,
                min_people=i * 100,
                max_people=i * 100 + 10,
                congestion="normal",
                is_replacement=False,
                observed_at=now,
                received_at=now,
                processed_at=now,
                processor_version="test",
            )
        )
    await session.execute(
        update(PopulationArea).where(PopulationArea.area_code == "POI004").values(is_active=False)
    )


def test_population_containment_holes_versions_readiness_and_savepoint(
    normalized_database, no_runtime_files
):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with write_session_factory(engine).begin() as session:
                await seed_population(session)
            async with read_session_factory(engine)() as session:
                service = AreaQueryService(AreaQueryRepository(session))
                result = await service.population_at(CENTER)
                assert [a["area_code"] for a in result["areas"]] == [
                    "POI001",
                    "POI002",
                    "POI003",
                    "POI005",
                ]
                assert [a["population"]["value"]["min_people"] for a in result["areas"][:2]] == [
                    100,
                    200,
                ]
                assert all(
                    a["population"]["reason_code"] == "GEOMETRY_VERSION_MISMATCH"
                    for a in result["areas"][2:]
                )
                hole = await service.population_at(Coordinates(latitude=37.562, longitude=126.972))
                assert hole["areas"] == [] and hole["reason_code"] == "OUTSIDE_OBSERVATION_AREA"
                other_part = await service.population_at(
                    Coordinates(latitude=37.60, longitude=127.01)
                )
                assert len(other_part["areas"]) == 4
                assert await session.scalar(text("SHOW transaction_read_only")) == "on"

                class BrokenRead(AreaQueryRepository):
                    async def ready(self, layer):
                        await self.session.execute(text("SELECT 1 / 0"))

                with pytest.raises(ServiceError) as error:
                    await AreaQueryService(BrokenRead(session)).population_at(CENTER)
                assert error.value.code == "DATA_UNAVAILABLE"
                assert await session.scalar(select(func.count()).select_from(PopulationArea)) == 5
            async with write_session_factory(engine).begin() as session:
                await session.execute(update(MapLayerVersion).values(is_ready=False))
            async with read_session_factory(engine)() as session:
                result = await AreaQueryService(AreaQueryRepository(session)).population_at(CENTER)
                assert all(
                    a["population"]["reason_code"] == "DATA_UNAVAILABLE" for a in result["areas"]
                )
            async with write_session_factory(engine).begin() as session:
                await session.execute(update(PopulationArea).values(is_active=False))
            async with read_session_factory(engine)() as session:
                result = await AreaQueryService(AreaQueryRepository(session)).population_at(CENTER)
                assert result["areas"] == [] and result["reason_code"] == "DATA_UNAVAILABLE"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_chat_current_facts_share_p03_cache_and_preserve_recommendation_state(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            env.app.state.settings.kakao_rest_api_key = SecretStr("test-key")
            env.kakao.directions.return_value = route_body()
            env.source.route.return_value = RouteEstimate(
                600, 1000, datetime.now(UTC) - timedelta(seconds=61)
            )
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            await parking_rows(env)
            async with env.app.state.write_session_factory.begin() as session:
                await seed_population(session)
            baseline = await counts(env)
            progress = await trip_state(env.app, original["trip_id"])
            before = deepcopy(
                (progress.search, progress.excluded, progress.conditions, progress.version)
            )
            for topic in ("travel_time", "parking", "population"):
                env.interpretation = Interpretation(intent=topic)
                question = chat(
                    following(original, first), message=f"현재 {topic}?", target=target(first)
                )
                result = (await post(env, C01, question)).json()["data"]
                assert result["kind"] == "answer" and result["answer"]["topic"] == topic
                assert result["result"] is None and result["condition_update"]["applied"] is False
                assert result["condition_update"]["conditions"] == question["current_conditions"]
                facts = result["answer"]["information"]
                if topic == "travel_time":
                    url, request = travel_request(original, first)
                    direct = (await post(env, url, request)).json()["data"]
                    assert direct["route"] == facts[0]
                    env.kakao.directions.assert_awaited_once()
                elif topic == "parking":
                    assert facts[0]["value"]["total_count"] == 25
                    assert facts[0]["value"]["availability"] == "unsupported"
                else:
                    assert [f["value"]["area_code"] for f in facts] == [
                        "POI001",
                        "POI002",
                        "POI003",
                        "POI005",
                    ]
                    assert facts[0]["value"]["population"]["min_people"] == 100
                    assert (
                        facts[2]["status"] == "unavailable"
                        and facts[2]["value"]["population"] is None
                    )
                assert (await post(env, C01, question)).json()["data"] == result
                assert await counts(env) == baseline
                progress = await trip_state(env.app, original["trip_id"])
                assert (
                    progress.search,
                    progress.excluded,
                    progress.conditions,
                    progress.version,
                ) == before
            env.kakao.directions.assert_awaited_once()
            env.kakao.region.assert_not_awaited()
            env.kakao.keyword.assert_not_awaited()
            assert env.source.search.await_count == 1

    asyncio.run(run())
