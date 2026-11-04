import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, RawPayload, RoadTrafficLatest
from app.repositories.collection import CollectionRepository
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration


def test_provider_history_failure_and_older_response_preserve_latest(
    normalized_database, stored_payload
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {
                                "id": "r1",
                                "properties": {},
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[127, 37.5], [127.01, 37.5]],
                                },
                            }
                        ]
                    },
                )
                await save_snapshot(s, "topis:r1", {"rows": [{"link_id": "r1", "prcs_spd": "0"}]})
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "topis:r1",
                    {"rows": [{"link_id": "r1", "prcs_spd": "80"}]},
                    received_at=datetime.now(UTC) - timedelta(days=1),
                )
                await save_snapshot(
                    s,
                    "topis_attempt:r1",
                    {"status": "failed", "error_code": "provider_network_error"},
                )
            async with factory() as s:
                latest = await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert latest.speed_kmh == 0 and latest.observed_at is None
                _, saved = await stored_payload(s, "topis_traffic", "link:r1")
                assert saved["rows"][0]["prcs_spd"] == "0"
                assert (
                    await CollectionRepository(s).latest_run("topis_traffic", "link:r1")
                ).status == "failed"
                assert await s.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 1
                assert await s.scalar(select(func.count()).select_from(RawPayload)) == 6
            # Invalid geometry is retained, but the failed normalization cannot erase good geometry.
            async with factory.begin() as s:
                result = await save_snapshot(
                    s,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {
                                "id": "r1",
                                "properties": {},
                                "geometry": {"type": "Point", "coordinates": [127, 37.5]},
                            }
                        ]
                    },
                )
            assert result.status == "failed" and result.error_code == "normalization_failed"
            async with factory() as s:
                failed_run = await s.get(CollectionRun, result.run_id)
                assert failed_run.status == result.status
                assert failed_run.error_code == result.error_code
                assert failed_run.accepted == 0 and failed_run.rejected == 1
                assert (await s.get(RawPayload, result.payload_id)).run_id == failed_run.id
                _, saved = await stored_payload(s, "topis_geometry", "file:topis_vertices")
                assert saved["features"][0]["geometry"]["type"] == "LineString"
                runs = list(
                    await s.scalars(
                        select(CollectionRun).where(CollectionRun.source == "topis_geometry")
                    )
                )

                counts = await CollectionRepository(s).rejection_counts([r.id for r in runs])
                assert {"normalization_failed": 1} in counts.values()
                assert await s.scalar(select(func.count()).select_from(RawPayload)) == 8
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boundary_and_link_mapping_conversion_preserves_relations(normalized_database):
    from app.models import AdministrativeArea, RegionBoundary, RoadLink, RoadLinkMapping

    polygon = {
        "type": "Polygon",
        "coordinates": [[[127, 37.5], [127.1, 37.5], [127.1, 37.6], [127, 37.5]]],
    }

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                areas = await save_snapshot(
                    session,
                    "sgis_areas",
                    {
                        "features": [
                            {
                                "id": "11010",
                                "properties": {"name": "테스트동", "sigungu_code": "1101"},
                                "geometry": polygon,
                            },
                            {
                                "id": "11011",
                                "properties": {"name": "상위 코드 없음"},
                                "geometry": polygon,
                            },
                        ]
                    },
                )
                boundary = await save_snapshot(
                    session,
                    "sgis_seoul_boundary",
                    {
                        "features": [{"geometry": polygon}],
                        "boundary_year": 2025,
                    },
                )
                roads = await save_snapshot(
                    session,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {
                                "id": "r1",
                                "properties": {
                                    "name": "테스트로",
                                    "direction": "동",
                                    "source_year": 2024,
                                },
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[127, 37.5], [127.01, 37.5]],
                                },
                            }
                        ],
                        "mapping_rows": [
                            {"service_link_id": "r1", "sequence": 0, "standard_link_id": "s1"},
                        ],
                    },
                )
            assert (areas.status, areas.error_code) == ("partial", "missing_sgis_parent_code")
            assert boundary.status == roads.status == "success"
            async with factory.begin() as session:
                malformed = await save_snapshot(
                    session, "sgis_areas", {"features": [{"properties": {"name": "경계 누락"}}]}
                )
            assert (malformed.status, malformed.error_code) == ("failed", "normalization_failed")
            async with factory() as session:
                area = await session.get(AdministrativeArea, "11010")
                assert area.sigungu_code == "1101" and area.last_payload_id == areas.payload_id
                assert await session.get(AdministrativeArea, "11011") is None
                assert (
                    await session.scalar(select(func.ST_GeometryType(AdministrativeArea.geometry)))
                    == "ST_MultiPolygon"
                )
                region = await session.get(RegionBoundary, ("sgis", "11"))
                assert (
                    region.source_version == "2025"
                    and region.last_payload_id == boundary.payload_id
                )
                road = await session.get(RoadLink, "r1")
                assert (road.name, road.direction, road.source_version) == (
                    "테스트로",
                    "동",
                    "2025",
                )
                mapping = await session.get(RoadLinkMapping, ("r1", 0, "s1"))
                assert (
                    mapping.source == "topis_standard_mapping"
                    and mapping.last_payload_id == roads.payload_id
                )
                assert await session.scalar(select(func.count()).select_from(RoadLinkMapping)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_snapshot_run_kinds_receipts_and_attempts_remain_distinct(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        receipt = datetime(2026, 10, 20, tzinfo=UTC)
        try:
            async with factory.begin() as session:
                collected = await save_snapshot(
                    session,
                    "fixture_provider",
                    {"datasets": {"parking": {"rows": [], "complete": False}}},
                    received_at=receipt,
                )
                imported = await save_snapshot(
                    session, "citydata_place_boundaries", {"features": []}
                )
                report = await save_snapshot(
                    session, "topis_collection_status", {"status": "complete"}
                )
                assert (
                    await save_snapshot(session, "fixture_provider:attempt", {"status": "partial"})
                    is None
                )
                assert (
                    await save_snapshot(session, "fixture_provider:attempt", {"status": "success"})
                    is None
                )
                failed = await save_snapshot(
                    session,
                    "fixture_provider:attempt",
                    {"status": "failed", "error_code": "provider_network_error"},
                )
            assert collected.status == "partial" and imported.status == report.status == "success"
            assert failed.status == "failed" and failed.payload_id is None
            async with factory() as session:
                for result, kind, payload_kind, received in (
                    (collected, "collection", "provider_response", receipt),
                    (imported, "import", "derived_snapshot", None),
                    (report, "reprocess", "processing_report", None),
                ):
                    record = await session.get(CollectionRun, result.run_id)
                    payload = await session.get(RawPayload, result.payload_id)
                    assert record.run_kind == kind and record.status == result.status
                    assert payload.payload_kind == payload_kind and payload.received_at == received
                    assert record.finished_at >= record.started_at
                partial = await session.get(CollectionRun, collected.run_id)
                assert partial.completeness == "partial" and partial.accepted == 1
                attempt = await session.get(CollectionRun, failed.run_id)
                assert attempt.error_code == "provider_network_error" and attempt.received == 0
                assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 4
                assert await session.scalar(select(func.count()).select_from(RawPayload)) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_distinguishes_failure_no_observation_and_zero(normalized_database):
    from app.models import MapRoadObservation
    from app.services.map_publication import MapPublicationService

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {
                                "id": "r1",
                                "properties": {},
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[127, 37.5], [127.01, 37.5]],
                                },
                            }
                        ]
                    },
                )
                await save_snapshot(s, "topis:r1", {"rows": [{"link_id": "r1", "prcs_spd": "0"}]})
                await MapPublicationService(s).publish("roads")
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "topis_attempt:r1",
                    {"status": "failed", "error_code": "provider_network_error"},
                )
                await MapPublicationService(s).publish("roads")
                r = await s.get(MapRoadObservation, "r1")
                assert r.has_observation and r.speed_kmh == 0 and r.collection_failed
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "topis_attempt:r1",
                    {"status": "failed", "error_code": "provider_no_observation"},
                )
                await MapPublicationService(s).publish("roads")
                r = await s.get(MapRoadObservation, "r1")
                assert not r.has_observation and r.speed_kmh is None and not r.collection_failed
        finally:
            await engine.dispose()

    asyncio.run(run())
