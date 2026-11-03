"""Internal reference reads use typed rows and receipts, independent of payload files."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RawPayload, RegionBoundary, RoadLink
from app.modules.data_pipeline.pipelines import topis
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository
from app.storage.payloads import configured_store
from jobs import scheduler

pytestmark = pytest.mark.integration
ROAD = {"type": "LineString", "coordinates": [[127, 37.5], [127.01, 37.5]]}
BOUNDARY = {
    "type": "Polygon",
    "coordinates": [
        [[127, 37.5], [127.1, 37.5], [127.1, 37.6], [127, 37.6], [127, 37.5]],
        [[127.02, 37.52], [127.08, 37.52], [127.08, 37.58], [127.02, 37.58], [127.02, 37.52]],
    ],
}


def forbid_file_reads(monkeypatch):
    def read(*args):
        pytest.fail("reference lookup must not open a payload file")

    monkeypatch.setattr(type(configured_store()), "load_json", read)


async def roads(session, links):
    result = await save_snapshot(
        session,
        "topis_geometry:2025",
        {
            "features": [{"id": link, "geometry": ROAD} for link in links],
        },
    )
    assert result.status == "success"
    return result


def test_current_road_import_excludes_leftover_and_inactive_links(normalized_database, monkeypatch):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                await roads(session, ["a", "b"])
                current = await roads(session, ["a", "c", "inactive"])
                inactive = await session.get(RoadLink, "inactive")
                inactive.is_active = False
                # A failed refresh must not make the last accepted reference disappear.
                failed = await save_snapshot(
                    session,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {"id": "a", "geometry": {"type": "Point", "coordinates": [127, 37.5]}}
                        ]
                    },
                )
                assert failed.status == "failed"
            forbid_file_reads(monkeypatch)
            async with factory() as session:
                repo = ReferenceRepository(session)
                assert await repo.topis_links() == ["a", "c"]
                assert await repo.has_prerequisite("topis_geometry:2025")
                assert (await session.get(RoadLink, "b")).is_active  # Leftover row really exists.
                assert (await session.get(RoadLink, "a")).last_payload_id == current.payload_id
            links, report = await topis.targets(engine, 1, True)
            assert links == ["a"] and report["total_links"] == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_old_reference_boundary_keeps_holes_and_year_without_payload_reads(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                saved = await save_snapshot(
                    session,
                    "sgis_seoul_boundary",
                    {
                        "features": [{"geometry": BOUNDARY}],
                        "boundary_year": 2020,
                    },
                )
                assert saved.status == "success"
                payload = await session.get(RawPayload, saved.payload_id)
                payload.stored_at = datetime(2020, 1, 1, tzinfo=UTC)
            forbid_file_reads(monkeypatch)
            async with factory() as session:
                repo = ReferenceRepository(session)
                boundary = await repo.seoul_boundary()
                assert boundary["year"] == 2020
                assert boundary["geometry"]["type"] == "MultiPolygon"
                spatial = SpatialRepository(session)
                assert await spatial.covers(boundary["geometry"], 127.01, 37.51)
                assert not await spatial.covers(boundary["geometry"], 127.05, 37.55)
                assert await repo.has_prerequisite("sgis_seoul_boundary")
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["topis_geometry:2025", "sgis_seoul_boundary"])
@pytest.mark.parametrize(
    "state", ["missing", "ready", "inactive", "empty_refresh", "failed_refresh"]
)
def test_scheduler_checks_usable_typed_rows_instead_of_file_presence(
    normalized_database, monkeypatch, source, state
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        calls = []
        is_road = source.startswith("topis")
        try:
            if state != "missing":
                async with factory.begin() as session:
                    saved = await save_snapshot(
                        session,
                        source,
                        {
                            "features": [{"id": "a", "geometry": ROAD if is_road else BOUNDARY}],
                            "boundary_year": 2025,
                        },
                    )
                    assert saved.status == "success"
                    if state == "inactive":
                        row = (
                            await session.get(RoadLink, "a")
                            if is_road
                            else await session.get(RegionBoundary, ("sgis", "11"))
                        )
                        row.is_active = False
                    elif state == "empty_refresh":
                        empty = await save_snapshot(session, source, {"features": []})
                        assert empty.status == "success"
                    elif state == "failed_refresh":
                        failed = await save_snapshot(
                            session,
                            source,
                            {
                                "features": [
                                    {
                                        "id": "a",
                                        "geometry": {"type": "Point", "coordinates": [127, 37.5]},
                                    }
                                ]
                            },
                        )
                        assert failed.status == "failed"
            forbid_file_reads(monkeypatch)

            async def command(task):
                calls.append(task)
                return 0

            monkeypatch.setattr(scheduler, "run_command", command)
            task = scheduler.Task("unused", 300, (), prerequisites=(source,))
            result = await scheduler.cycle(engine, "reference-test", task)
            ready = state in {"ready", "failed_refresh"}
            assert result is (True if ready else None)
            assert len(calls) == int(ready)
            # Empty successful receipts must not revive rows left by an older import.
            if state == "empty_refresh":
                async with factory() as session:
                    assert (
                        await session.scalar(select(RoadLink if is_road else RegionBoundary))
                        is not None
                    )
                    repo = ReferenceRepository(session)
                    current = await repo.topis_links() if is_road else await repo.seoul_boundary()
                    assert current == ([] if is_road else None)
        finally:
            await engine.dispose()

    asyncio.run(run())
