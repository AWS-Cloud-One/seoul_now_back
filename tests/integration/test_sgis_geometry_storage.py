"""SGIS geometry validation and atomic canonical writes without response archives."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AdministrativeArea
from app.modules.data_pipeline.pipelines import supplemental
from app.modules.data_pipeline.pipelines.boundaries import prepare_areas, save_areas
from app.repositories import boundaries as repository
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]

VALID = "POLYGON((953000 1950000,954000 1950000,954000 1951000,953000 1951000,953000 1950000))"
AREA_CHANGE = (
    "POLYGON((953000 1950000,954000 1951000,953000 1951000,953750 1950250,953000 1950000))"
)
BOUNDARY = {
    "type": "Polygon",
    "coordinates": [
        [
            [953000, 1950000],
            [954000, 1950000],
            [954000, 1951000],
            [953000, 1951000],
            [953000, 1950000],
        ]
    ],
}


def collected(rows, boundary=BOUNDARY):
    return {
        "complete": True,
        "areas": [
            [
                {"cd": "11010", "addr_name": "테스트구"},
                {
                    "cd": code,
                    "addr_name": "테스트동" + code,
                    "full_addr": "서울 테스트구 테스트동",
                    "pg": wkt,
                },
            ]
            for code, wkt in rows
        ],
        "boundary": {"features": [{"geometry": boundary}]},
    }


async def store(factory, source):
    async with factory.begin() as session:
        return await save_areas(session, source)


def test_repeated_input_skips_geometry_update_and_road_relation_rebuild(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        statements = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda conn, cursor, sql, *args: statements.append(sql),
        )
        source = collected([("11010510", VALID)])
        try:
            monkeypatch.setattr(
                supplemental.collectors, "collect_areas", AsyncMock(return_value=source)
            )
            result = await supplemental.run_supplemental(engine, None, None, "areas")
            assert result["status"] == "success"
            async with factory() as session:
                previous = await session.get(AdministrativeArea, "11010510")
                before = (previous.boundary_version, str(previous.geometry))
            statements.clear()
            assert (await store(factory, source))["status"] == "success"
            writes = [
                sql
                for sql in statements
                if sql.startswith(("UPDATE administrative_areas", "UPDATE region_boundaries"))
            ]
            assert len(writes) == 2 and all(
                "geometry=" not in sql.split("WHERE")[0] for sql in writes
            )
            assert not any(sql.startswith("DELETE FROM road_area_mappings") for sql in statements)
            async with factory() as session:
                current = await session.get(AdministrativeArea, "11010510")
                assert (current.boundary_version, str(current.geometry)) == before
                assert await ReferenceRepository(session).has_prerequisite("sgis_seoul_boundary")
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "replacement,reason",
    [
        ("POLYGON((broken))", "invalid_polygon_input"),
        (None, "non_polygon_geometry"),
        ("POINT(953000 1950000)", "non_polygon_geometry"),
        ("POLYGON EMPTY", "empty_or_degenerate_polygon"),
        (AREA_CHANGE, "polygon_repair_changes_area"),
    ],
)
def test_rejected_replacement_keeps_old_area_and_stores_other_rows(
    normalized_database, replacement, reason
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            async with factory() as session:
                previous = await session.get(AdministrativeArea, "11010510")
                before = (previous.boundary_version, str(previous.geometry))
            result = await store(
                factory, collected([("11010510", replacement), ("11010520", VALID)])
            )
            assert result["status"] == "partial" and result["records"] == 2  # one dong + Seoul
            assert result["geometry_quality"]["rows"][0]["reason"] == reason
            async with factory() as session:
                previous = await session.get(AdministrativeArea, "11010510")
                assert (previous.boundary_version, str(previous.geometry)) == before
                assert previous.is_active and await session.get(AdministrativeArea, "11010520")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_seoul_boundary_keeps_previous_and_accepts_current_dongs(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            async with factory() as session:
                before = await ReferenceRepository(session).seoul_boundary()
            result = await store(
                factory,
                collected(
                    [("11010520", VALID)], {"type": "Point", "coordinates": [953000, 1950000]}
                ),
            )
            assert result["status"] == "partial" and result["boundary_quality"]["rejected"] == 1
            async with factory() as session:
                assert await ReferenceRepository(session).seoul_boundary() == before
                assert await session.get(AdministrativeArea, "11010520")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boundary_relation_failure_rolls_back_both_and_readers_keep_committed_values(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            async with factory.begin() as session:
                await session.execute(
                    text("""
                    INSERT INTO data_sources(source_code,display_name,provider_name)
                    VALUES ('topis_geometry','test','test') ON CONFLICT DO NOTHING
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO road_links
                        (link_id,source,source_version,geometry,processed_at,processor_version)
                    SELECT 'road','topis_geometry','test',
                        ST_Transform(ST_GeomFromText(
                            'LINESTRING(953100 1950100,953900 1950900)',5179),4326),now(),'test'
                """)
                )
            # A changed canonical polygon creates relations to the road.
            changed = collected([("11010510", VALID), ("11010520", VALID)])
            assert (await store(factory, changed))["status"] == "success"
            async with factory() as session:
                before = await ReferenceRepository(session).seoul_boundary()
                mappings = (
                    await session.execute(
                        text(
                            "SELECT link_id,dong_code,boundary_version "
                            "FROM road_area_mappings ORDER BY dong_code"
                        )
                    )
                ).all()
                assert len(mappings) == 2
            replacement = collected([("11010530", VALID)])
            original = repository.replace_administrative_areas

            def fail_after_mapping(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected after relation rebuild")

            monkeypatch.setattr(repository, "replace_administrative_areas", fail_after_mapping)
            result = await store(factory, replacement)
            assert (
                result["status"] == "failed" and result["error_code"] == "boundary_storage_failed"
            )
            async with factory() as session:
                assert await ReferenceRepository(session).seoul_boundary() == before
                assert await session.get(AdministrativeArea, "11010530") is None
                assert (
                    await session.execute(
                        text(
                            "SELECT link_id,dong_code,boundary_version "
                            "FROM road_area_mappings ORDER BY dong_code"
                        )
                    )
                ).all() == mappings
            monkeypatch.setattr(repository, "replace_administrative_areas", original)
            async with factory.begin() as writer:
                result = await save_areas(writer, replacement)
                assert result["status"] == "success"
                async with factory() as reader:
                    assert await reader.get(AdministrativeArea, "11010530") is None
                    assert (await reader.get(AdministrativeArea, "11010510")).is_active
            async with factory() as reader:
                assert (await reader.get(AdministrativeArea, "11010530")).is_active
                assert not (await reader.get(AdministrativeArea, "11010510")).is_active
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_duplicate_identity_is_rejected_without_deactivating_prior_areas(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            source = collected([("11010520", VALID), ("11010520", VALID), ("11010530", VALID)])
            async with factory() as session:
                data, boundary_data = await prepare_areas(SpatialRepository(session), source)
                assert "source_input" not in data and "source_input" not in boundary_data
            result = await store(factory, source)
            assert result["status"] == "partial" and result["geometry_quality"]["rejected"] == 2
            async with factory() as session:
                assert (await session.get(AdministrativeArea, "11010510")).is_active
                assert await session.get(AdministrativeArea, "11010520") is None
                assert await session.get(AdministrativeArea, "11010530")
        finally:
            await engine.dispose()

    asyncio.run(run())
