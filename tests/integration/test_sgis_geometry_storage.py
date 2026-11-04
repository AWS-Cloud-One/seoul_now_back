"""SGIS polygon quality is checked before writes; rejected replacements retain prior boundaries."""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AdministrativeArea, RawPayload, RegionBoundary
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.modules.data_pipeline.pipelines.supplemental import prepare_areas
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration

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
        data, boundary = await prepare_areas(SpatialRepository(session), source)
        boundary_result = await save_snapshot(session, "sgis_seoul_boundary", boundary)
        area_result = await save_snapshot(session, "sgis_areas", data)
    return data, boundary, area_result, boundary_result


def test_valid_polygon_and_repeated_input_keep_geometry_identity_and_evidence(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        source = collected([("11010510", VALID)])
        try:
            data, boundary, first, _ = await store(factory, source)
            assert data["features"][0]["geometry"]["type"] == "Polygon"
            assert boundary["features"][0]["geometry"]["type"] == "Polygon"
            assert first.status == "success"
            assert data["geometry_quality"]["accepted"] == 1
            assert data["geometry_quality"]["repaired"] == 0
            async with factory() as session:
                old = await session.get(AdministrativeArea, "11010510")
                previous_version = old.boundary_version
                previous_geometry = str(old.geometry)
                raw = await session.get(RawPayload, first.payload_id)
                stored = configured_store().load_json(raw)
                assert stored["source_input"] == source["areas"]
                assert stored["geometry_quality"]["rows"][0]["status"] == "valid"
            _, _, repeated, _ = await store(factory, source)
            assert repeated.status == "success"
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(AdministrativeArea)) == 1
                )
                current = await session.get(AdministrativeArea, "11010510")
                assert current.boundary_version == previous_version
                assert str(current.geometry) == previous_geometry
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
def test_rejected_replacement_keeps_old_area_and_accepts_other_rows(
    normalized_database, replacement, reason
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            async with factory() as session:
                previous = await session.get(AdministrativeArea, "11010510")
                original_payload = previous.last_payload_id
                original_geometry = str(previous.geometry)
            source = collected([("11010510", replacement), ("11010520", VALID)])
            data, _, result, _ = await store(factory, source)
            assert result.status == "partial" and result.error_code == "partial_boundary_geometry"
            assert result.processing["received"] == 2
            assert result.processing["accepted"] == result.processing["rejected"] == 1
            rejected = data["geometry_quality"]["rows"][0]
            assert rejected["code"] == "11010510" and rejected["reason"] == reason
            if replacement == AREA_CHANGE:
                assert rejected["relative_area_change"] == 2.0
                assert rejected["area_before_m2"] == 125000.0
                assert rejected["area_after_m2"] == 375000.0
            async with factory() as session:
                previous = await session.get(AdministrativeArea, "11010510")
                assert previous.last_payload_id == original_payload
                assert str(previous.geometry) == original_geometry
                added = await session.get(AdministrativeArea, "11010520")
                assert added.last_payload_id == result.payload_id
                assert (
                    await session.scalar(select(func.count()).select_from(AdministrativeArea)) == 2
                )
                raw = await session.get(RawPayload, result.payload_id)
                archived = configured_store().load_json(raw)
                assert archived["source_input"][0][1]["pg"] == replacement
                assert archived["geometry_quality"]["rows"][0]["reason"] == reason
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unusable_seoul_boundary_preserves_prior_boundary_and_new_valid_area(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, collected([("11010510", VALID)]))
            async with factory() as session:
                prior = await ReferenceRepository(session).seoul_boundary()
            invalid_boundary = {
                "type": "LineString",
                "coordinates": [[953000, 1950000], [954000, 1951000]],
            }
            _, data, area_result, boundary_result = await store(
                factory, collected([("11010520", VALID)], invalid_boundary)
            )
            assert area_result.status == "success"
            assert (
                boundary_result.status == "failed"
                and boundary_result.error_code == "no_usable_boundary"
            )
            assert data["geometry_quality"]["rejected"] == 1 and not data["features"]
            async with factory() as session:
                assert await ReferenceRepository(session).seoul_boundary() == prior
                current = await session.get(RegionBoundary, ("sgis", "11"))
                assert str(current.last_payload_id) == prior["payload_id"]
                assert await session.get(AdministrativeArea, "11010520") is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
