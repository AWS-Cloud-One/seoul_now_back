"""Observe actual spatial-column writes, not merely equal results after a rewrite."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import MapFeature, ParkingFacility, ParkingSourceRecord, PopulationArea
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.modules.data_pipeline.pipelines.sdot import save_sdot
from app.services.map_publication import MapPublicationService
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot
from tests.fixtures.sdot import collected, location, observation, sensor_file

pytestmark = pytest.mark.integration
POLYGON = {
    "type": "Polygon",
    "coordinates": [[[126.9, 37.4], [127.2, 37.4], [127.2, 37.7], [126.9, 37.7], [126.9, 37.4]]],
}
LINE = {"type": "LineString", "coordinates": [[127, 37.5], [127.01, 37.5]]}


async def audit(session, tables):
    await session.execute(
        text("CREATE TABLE spatial_write_audit (relation text, identity text, operation text)")
    )
    await session.execute(
        text("""CREATE FUNCTION spatial_write_audit_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO spatial_write_audit VALUES
            (TG_TABLE_NAME, to_jsonb(NEW)->>TG_ARGV[0], TG_OP);
        RETURN NEW; END $$""")
    )
    for table, column, identity in tables:
        await session.execute(
            text(
                f"CREATE TRIGGER audit_spatial AFTER UPDATE OF {column} ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION spatial_write_audit_fn('{identity}')"
            )
        )


async def events(factory):
    async with factory() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT relation,identity FROM spatial_write_audit "
                        "ORDER BY relation,identity"
                    )
                )
            ).all()
        )


def test_parking_same_position_is_never_written_but_real_move_keeps_identity(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = [
            dict(
                PKLT_CD=str(i),
                PKLT_NM=f"parking{i}",
                ADDR=f"서울 address{i}",
                LAT=37.56,
                LOT=126.97 + i / 1000,
                TPKCT="100",
            )
            for i in (1, 2)
        ]

        async def fetch():
            return deepcopy(rows)

        try:
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 2
            async with factory.begin() as session:
                await audit(
                    session,
                    [
                        ("parking_facilities", "location", "parking_id"),
                        ("parking_source_records", "location", "source_record_id"),
                        ("map_features", "geometry", "feature_id"),
                    ],
                )
                old = {
                    r.provider_record_id: (r.source_record_id, r.parking_id, r.last_payload_id)
                    for r in await session.scalars(select(ParkingSourceRecord))
                }
                original_features = {
                    r.feature_id: r.published_at for r in await session.scalars(select(MapFeature))
                }
            rows[0]["TPKCT"] = "101"
            result = await run_parking(engine, fetch, SOURCE)
            assert result["accepted"] == 2 and await events(factory) == []
            async with factory() as session:
                record = await session.get(ParkingSourceRecord, old["1"][0])
                assert record.registered_capacity == 101 and record.last_payload_id != old["1"][2]
                assert {
                    r.feature_id: r.published_at for r in await session.scalars(select(MapFeature))
                } == original_features
            rows[0]["LOT"] += 0.001
            result = await run_parking(engine, fetch, SOURCE)
            assert result["accepted"] == 2
            assert await events(factory) == sorted(
                [
                    ("parking_facilities", str(old["1"][1])),
                    ("parking_source_records", str(old["1"][0])),
                    ("map_features", str(old["1"][1])),
                ]
            )
            async with factory() as session:
                assert {
                    r.provider_record_id: (r.source_record_id, r.parking_id)
                    for r in await session.scalars(select(ParkingSourceRecord))
                } == {k: v[:2] for k, v in old.items()}
                unchanged = await session.get(MapFeature, ("parking", str(old["2"][1])))
                assert unchanged.published_at == original_features[str(old["2"][1])]
                assert await session.get(ParkingFacility, old["1"][1]) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,table,key",
    [
        ("topis_geometry:2025", "road_links", "link_id"),
        ("citydata_place_boundaries", "population_areas", "area_code"),
        ("sgis_areas", "administrative_areas", "dong_code"),
        ("sgis_seoul_boundary", "region_boundaries", "region_code"),
    ],
)
def test_reference_geometry_is_changed_only_for_different_ordered_coordinates(
    normalized_database, source, table, key
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        data = {
            "features": [
                {
                    "id": "one",
                    "properties": {"name": "before", "sigungu_code": "district"},
                    "geometry": deepcopy(LINE if table == "road_links" else POLYGON),
                }
            ]
        }
        try:
            async with factory.begin() as session:
                assert (await save_snapshot(session, source, data)).status == "success"
                await audit(session, [(table, "geometry", key)])
            data["features"][0]["properties"]["name"] = "after"
            async with factory.begin() as session:
                assert (await save_snapshot(session, source, data)).status == "success"
            assert await events(factory) == []
            geometry = data["features"][0]["geometry"]
            if table == "road_links":
                # Reversing a road changes its ordered representation even with identical coverage.
                geometry["coordinates"].reverse()
            else:
                geometry["coordinates"][0][1][0] += 0.01
            async with factory.begin() as session:
                assert (await save_snapshot(session, source, data)).status == "success"
            assert await events(factory) == [
                (table, "11" if table == "region_boundaries" else "one")
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sensor_observations_update_without_rewriting_installation_position(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        stamp = datetime(2026, 11, 4, 1, tzinfo=UTC)
        file = sensor_file([location(), location("2")])

        async def save(step, count):
            async with factory.begin() as session:
                return await save_sdot(
                    session,
                    file,
                    collected(
                        [
                            observation(
                                VISITOR_COUNT=str(count), DATE=f"2026-11-04_10:{step:02d}:00"
                            )
                        ],
                        (stamp + timedelta(minutes=step)).isoformat(),
                    ),
                    started_at=stamp + timedelta(minutes=step),
                )

        try:
            async with factory.begin() as session:
                await save_snapshot(
                    session,
                    "sgis_seoul_boundary",
                    {"features": [{"geometry": POLYGON}], "boundary_year": 2025},
                )
            assert (await save(0, 0))["storage"]["sensors_written"] == 2
            async with factory.begin() as session:
                await audit(session, [("pedestrian_sensors", "geometry", "sensor_code")])
            assert (await save(1, 2))["storage"]["observations_written"] == 1
            assert await events(factory) == []
            file = sensor_file([location(F="127.001"), location("2")])
            await save(2, 3)
            assert await events(factory) == [("pedestrian_sensors", "1")]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_map_reference_edit_keeps_unaffected_rows_and_updates_properties_without_geometry(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        data = {
            "features": [
                {"id": code, "properties": {"name": code}, "geometry": deepcopy(POLYGON)}
                for code in ("a", "b")
            ]
        }

        async def save():
            async with factory.begin() as session:
                await save_snapshot(session, "citydata_place_boundaries", data)
                await MapPublicationService(session).publish("population")

        async def feature_state():
            async with factory() as session:
                return dict(
                    (
                        await session.execute(
                            text(
                                "SELECT feature_id, xmin::text FROM map_features "
                                "WHERE layer='population'"
                            )
                        )
                    ).all()
                )

        try:
            await save()
            original = await feature_state()
            async with factory.begin() as session:
                await audit(session, [("map_features", "geometry", "feature_id")])
            data["features"][0]["properties"]["name"] = "renamed"
            await save()
            renamed = await feature_state()
            assert renamed["b"] == original["b"] and renamed["a"] != original["a"]
            assert await events(factory) == []
            data["features"][0]["geometry"]["coordinates"][0][1][0] += 0.01
            await save()
            assert await events(factory) == [("map_features", "a")]
            assert (await feature_state())["b"] == original["b"]
            data["features"] = [
                data["features"][1],
                {"id": "c", "properties": {"name": "new"}, "geometry": POLYGON},
            ]
            async with factory.begin() as session:
                await session.execute(
                    update(PopulationArea)
                    .where(PopulationArea.area_code == "a")
                    .values(is_active=False)
                )
            await save()
            final = await feature_state()
            assert set(final) == {"b", "c"} and final["b"] == original["b"]
        finally:
            await engine.dispose()

    asyncio.run(run())
