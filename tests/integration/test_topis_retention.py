"""Log retention and interrupted cycles use only committed typed database state."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, RoadTrafficAttemptLatest, RoadTrafficLatest
from app.modules.data_pipeline.pipelines import topis, topis_storage
from app.repositories.collection import CollectionRepository
from tests.fixtures.topis import entry, response, seed_roads

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]
LINKS = ["1000000000", "1000000001", "1000000002"]


def test_unexpected_worker_failure_cancels_requests_and_releases_lease(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        started, cancelled = asyncio.Event(), []

        async def fetch(link):
            if link == "first":
                await started.wait()
                raise RuntimeError("unexpected worker failure")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(link)

        try:
            with pytest.raises(ExceptionGroup):
                await topis.run_topis(engine, fetch, ["first", "second"])
            assert cancelled == ["second"]
            assert (await topis_storage.maintain_cycles(engine, recover=True))[
                "status"
            ] == "success"
            async with async_sessionmaker(engine)() as s:
                run = await s.scalar(
                    select(CollectionRun).where(CollectionRun.source == "topis_traffic")
                )
                assert run.status == "failed" and run.error_code == "collection_interrupted"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_stop_cancels_a_pending_retry(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "RETRY_DELAYS", (0.01, 0.01))

    async def run():
        engine = create_async_engine(normalized_database)
        transient_seen, calls = asyncio.Event(), []

        async def fetch(link):
            calls.append(link)
            if link == "first":
                transient_seen.set()
                raise CollectionError("provider_network_error")
            await transient_seen.wait()
            raise CollectionError("provider_quota_exceeded")

        try:
            result = await topis.run_topis(engine, fetch, ["first", "second"])
            assert calls == ["first", "second"]
            assert result["status"] == "paused_provider_limit" and result["retry_attempts"] == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_twenty_cycles_do_not_remove_last_good_values_or_current_attempts(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS)

            async def good(link):
                return response(link)

            first = await topis.run_topis(engine, good, LINKS)
            async with factory() as s:
                receipt = (await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1]))).received_at

            async def mixed(link):
                if link == LINKS[1]:
                    raise CollectionError("provider_http_error")
                return response(link, 40)

            for _ in range(21):
                result = await topis.run_topis(engine, mixed, LINKS[:2])
                assert result["status"] == "partial"
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(CollectionRun)) == 20
                assert await s.get(CollectionRun, UUID(first["run_id"])) is None
                assert (
                    await s.scalar(select(func.count()).select_from(RoadTrafficAttemptLatest)) == 3
                )
                old = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1]))
                assert old.speed_kmh == 25 and old.received_at == receipt
                assert (await s.get(RoadTrafficAttemptLatest, LINKS[1])).status == "failed"
                untouched = await s.get(RoadTrafficAttemptLatest, LINKS[2])
                assert untouched.status == "success" and untouched.run_id is None
                assert await topis.targets(engine, 1, True) == (
                    [LINKS[2]],
                    {
                        "scope": "scheduled_official_batch",
                        "total_links": 3,
                        "selected_links": 1,
                        "truncated": True,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_interrupted_run_closes_from_committed_counts_without_replay(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        receipt = datetime.now(UTC) - timedelta(minutes=10)
        try:
            await seed_roads(factory, LINKS)
            async with factory.begin() as s:
                run = await CollectionRepository(s).start("topis_traffic", "network")
                await topis_storage.store_batch(
                    s, run.id, [entry(LINKS[0], response(LINKS[0]), receipt)]
                )
            with pytest.raises(RuntimeError):
                async with factory.begin() as s:
                    await topis_storage.store_batch(
                        s, run.id, [entry(LINKS[1], response(LINKS[1]), receipt)]
                    )
                    raise RuntimeError("process lost before commit")
            result = await topis_storage.maintain_cycles(engine, recover=True)
            assert result["runs_recovered"] == 1
            assert (await topis_storage.maintain_cycles(engine, recover=True))[
                "runs_recovered"
            ] == 0
            async with factory() as s:
                current = await s.get(CollectionRun, run.id)
                assert (current.received, current.accepted, current.rejected) == (1, 1, 0)
                assert (
                    current.status == "partial" and current.error_code == "collection_interrupted"
                )
                value = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0]))
                assert value.received_at == receipt and value.speed_kmh == 25
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1])) is None
                assert await s.get(RoadTrafficAttemptLatest, LINKS[1]) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancel_keeps_committed_batch_and_releases_source_lock(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "BATCH_SIZE", 1)
    monkeypatch.setattr(topis, "CONCURRENCY", 1)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        waiting = asyncio.Event()
        try:
            await seed_roads(factory, LINKS[:2])

            async def fetch(link):
                if link == LINKS[1]:
                    waiting.set()
                    await asyncio.Event().wait()
                return response(link)

            task = asyncio.create_task(topis.run_topis(engine, fetch, LINKS[:2]))
            await asyncio.wait_for(waiting.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with factory() as s:
                run = await s.scalar(select(CollectionRun))
                assert run.status == "partial" and run.accepted == run.received == 1
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0]))
                assert await s.get(RoadTrafficAttemptLatest, LINKS[1]) is None
            assert (await topis_storage.maintain_cycles(engine, recover=True))[
                "status"
            ] == "success"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_attempt_write_failure_cannot_commit_the_observation_alone(
    normalized_database, monkeypatch
):
    original = topis_storage.write_attempt
    failed = False

    async def fail_once(session, run_id, outcome):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("attempt storage failure")
        await original(session, run_id, outcome)

    monkeypatch.setattr(topis_storage, "write_attempt", fail_once)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS[:2])
            async with factory.begin() as s:
                run = await CollectionRepository(s).start("topis_traffic", "network")
                results = await topis_storage.store_batch(
                    s,
                    run.id,
                    [entry(link, response(link), datetime.now(UTC)) for link in LINKS[:2]],
                )
                assert [r["status"] for r in results] == ["failed", "success"]
                run_id = run.id
            async with factory() as s:
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0])) is None
                assert (await s.get(RoadTrafficAttemptLatest, LINKS[0])).status == "failed"
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1]))
                run = await s.get(CollectionRun, run_id)
                assert (run.received, run.accepted, run.rejected) == (2, 1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_timer_flush_finishes_once_when_last_worker_exits(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "BATCH_SECONDS", 0.001)
    monkeypatch.setattr(topis, "CONCURRENCY", 1)
    writing, release = asyncio.Event(), asyncio.Event()
    original = topis.store_batch
    counts = []

    async def slow_batch(session, run_id, entries):
        counts.append(len(entries))
        writing.set()
        await release.wait()
        return await original(session, run_id, entries)

    monkeypatch.setattr(topis, "store_batch", slow_batch)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS[:2])

            async def fetch(link):
                if link == LINKS[1]:
                    await asyncio.wait_for(writing.wait(), 5)
                    release.set()
                return response(link)

            result = await asyncio.wait_for(topis.run_topis(engine, fetch, LINKS[:2]), 10)
            assert result["status"] == "success" and result["completed"] == sum(counts) == 2
            async with factory() as s:
                run = await s.scalar(select(CollectionRun))
                assert run.received == run.accepted == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_batch_summary_failure_rolls_back_new_values_and_keeps_previous_commit(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "BATCH_SIZE", 1)
    monkeypatch.setattr(topis, "CONCURRENCY", 1)
    original = topis_storage.record_batch_counts
    calls = 0

    async def fail_second(session, run_id, entries):
        nonlocal calls
        calls += 1
        await original(session, run_id, entries)
        if calls == 2:
            raise RuntimeError("summary failure")

    monkeypatch.setattr(topis_storage, "record_batch_counts", fail_second)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS[:2])

            async def fetch(link):
                return response(link)

            with pytest.raises(ExceptionGroup):
                await topis.run_topis(engine, fetch, LINKS[:2])
            assert calls == 2  # A possibly committed batch is never retried in finally.
            async with factory() as s:
                run = await s.scalar(select(CollectionRun))
                assert (run.received, run.accepted, run.rejected) == (1, 1, 0)
                assert run.error_code == "collection_interrupted"
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0]))
                assert await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1])) is None
                assert await s.get(RoadTrafficAttemptLatest, LINKS[1]) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("normalized_database", ["0011"], indirect=True)
def test_cutover_preserves_old_per_link_attempts_without_replacing_newer_state(normalized_database):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine(normalized_database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO data_sources(source_code,display_name,provider_name)
                VALUES ('topis_geometry','test','test'),('topis_traffic','test','test')
            """)
            )
            conn.execute(
                text("""
                INSERT INTO road_links
                    (link_id,source,source_version,geometry,processor_version,processed_at)
                SELECT link,'topis_geometry','test',
                    ST_GeomFromText('MULTILINESTRING((127 37.5,127.01 37.5))',4326),'test',now()
                FROM (VALUES ('old'),('newer')) AS v(link)
            """)
            )
            conn.execute(
                text("""
                INSERT INTO collection_runs
                    (id,source,target_key,started_at,finished_at,status,error_code)
                VALUES
                ('00000000-0000-0000-0000-000000000011','topis_traffic','link:old',
                 '2026-11-04T00:00:00Z','2026-11-04T00:01:00Z','failed','provider_http_error'),
                ('00000000-0000-0000-0000-000000000012','topis_traffic','link:newer',
                 '2026-11-04T00:00:00Z','2026-11-04T00:01:00Z','failed','provider_http_error')
            """)
            )
            conn.execute(
                text("""
                INSERT INTO road_traffic_attempt_latest
                    (link_id,attempted_at,status,error_code,attempts,observation_updated)
                VALUES ('newer','2026-11-04T00:02:00Z','success',NULL,2,true)
            """)
            )
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        with engine.connect() as conn:
            states = {
                row["link_id"]: row
                for row in conn.execute(
                    text("SELECT * FROM road_traffic_attempt_latest")
                ).mappings()
            }
            assert states["old"]["status"] == "failed" and not states["old"]["observation_updated"]
            assert states["old"]["attempted_at"] == datetime(2026, 11, 4, 0, 1, tzinfo=UTC)
            assert states["newer"]["status"] == "success" and states["newer"]["attempts"] == 2
    finally:
        engine.dispose()
