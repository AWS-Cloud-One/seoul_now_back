"""TOPIS direct batches preserve concurrency, retries, committed counts and provider stops."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, RoadTrafficAttemptLatest, RoadTrafficLatest
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.pipelines import topis
from app.repositories.collection import CollectionRepository
from tests.fixtures.topis import response, seed_roads, write

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


def watch_transactions(engine):
    owners = {}

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        owners[id(connection)] = asyncio.current_task()

    @event.listens_for(engine.sync_engine, "commit")
    @event.listens_for(engine.sync_engine, "rollback")
    def end(connection):
        owners.pop(id(connection), None)

    return owners


def test_rotation_reads_latest_attempts_and_all_scope_is_unbounded(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(4)]
        try:
            await seed_roads(factory, links)
            for i, error in enumerate((None, "provider_http_error", "provider_no_observation")):
                await write(
                    factory,
                    links[i],
                    response(links[i]),
                    datetime(2026, 1, i + 1, tzinfo=UTC),
                    error=error,
                )
            selected, report = await topis.targets(engine, 2, True)
            assert selected == [links[3], links[0]] and report["truncated"]
            assert (await topis.targets(engine, 1))[0] == links
            owners, calls = watch_transactions(engine), []

            async def fetch(link):
                assert asyncio.current_task() not in owners.values()
                calls.append(link)
                return response(link)

            result = await topis.run_topis_network(engine, fetch, 2, True)
            assert result["status"] == "success" and calls == selected and not owners
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parallel_cycle_has_one_log_and_bounded_typed_batches(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    sizes, buffered = [], []
    original = topis.store_batch

    async def inspect_batch(session, run_id, entries):
        sizes.append(len(entries))
        assert all("data" not in entry and "rows" not in entry for entry in entries)
        outcomes = await original(session, run_id, entries)
        assert all("values" not in outcome for outcome in outcomes)
        buffered.extend(outcomes)
        return outcomes

    monkeypatch.setattr(topis, "store_batch", inspect_batch)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(55)]
        calls, active, peak = [], 0, 0
        ready = asyncio.Event()
        try:
            await seed_roads(factory, [link for link in links if link != links[3]])
            owners = watch_transactions(engine)

            async def fetch(link):
                nonlocal active, peak
                assert asyncio.current_task() not in owners.values()
                calls.append(link)
                active += 1
                peak = max(peak, active)
                if active == 8:
                    ready.set()
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    if link == links[1]:
                        raise CollectionError("provider_no_observation")
                    if link == links[2]:
                        raise CollectionError("provider_http_error")
                    return response(link, 0, 0, unselected="discard me")
                finally:
                    active -= 1

            result = await topis.run_topis(engine, fetch, links + links[:2])
            assert peak == 8 and active == 0 and calls == links
            assert result["status"] == "partial" and result["cycle_complete"]
            assert result["counts"] == {
                "success": 52,
                "provider_no_observation": 1,
                "provider_http_error": 1,
                "missing_road_geometry": 1,
            }
            assert sum(sizes) == 55 and max(sizes) <= 50 and len(buffered) == 55
            async with factory() as s:
                runs = list(await s.scalars(select(CollectionRun)))
                assert len(runs) == 1
                assert (runs[0].received, runs[0].accepted, runs[0].rejected) == (55, 52, 3)
                assert await s.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 52
                assert (
                    await s.scalar(select(func.count()).select_from(RoadTrafficAttemptLatest)) == 54
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retry_policy_and_exhausted_failure_preserve_last_good(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "RETRY_DELAYS", (0, 0))

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(4)]
        calls = Counter()
        receipt = datetime.now(UTC) - timedelta(minutes=1)
        try:
            await seed_roads(factory, links)
            await write(factory, links[2], response(links[2], 12, 40), receipt)
            owners = watch_transactions(engine)

            async def handler(request):
                assert asyncio.current_task() not in owners.values()
                link = request.url.path.rsplit("/", 1)[1]
                calls[link] += 1
                if link == links[1] and calls[link] == 1:
                    return httpx.Response(503)
                if link == links[2] or link == links[1] and calls[link] == 2:
                    raise httpx.ReadTimeout("private URL", request=request)
                if link == links[3]:
                    return httpx.Response(200, text="<RESULT><CODE>INFO-200</CODE></RESULT>")
                return httpx.Response(
                    200,
                    text=f"<TrafficInfo><RESULT><CODE>INFO-000</CODE></RESULT><row><link_id>{link}</link_id><prcs_spd>25</prcs_spd><prcs_trv_time>30</prcs_trv_time></row></TrafficInfo>",
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await topis.run_topis(
                    engine, lambda link: collect_topis(client, "test", link), links
                )
            assert dict(calls) == dict(zip(links, (1, 3, 3, 1), strict=True))
            assert result["retry_attempts"] == 4 and result["status"] == "partial"
            assert result["counts"] == {
                "success": 2,
                "provider_network_error": 1,
                "provider_no_observation": 1,
            }
            async with factory() as s:
                value = await s.get(RoadTrafficLatest, ("topis_traffic", links[2]))
                assert value.speed_kmh == 12 and value.received_at == receipt
                attempt = await s.get(RoadTrafficAttemptLatest, links[2])
                assert (
                    attempt.status == "failed"
                    and attempt.attempts == 3
                    and not attempt.observation_updated
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("error", sorted(topis.STOP_ERRORS))
def test_provider_stop_does_not_start_remaining_requests(normalized_database, monkeypatch, error):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(30)]
        calls = []
        try:
            await seed_roads(factory, links[:1])
            await write(factory, links[0], response(links[0], 12, 40), datetime.now(UTC))

            async def fetch(link):
                calls.append(link)
                raise CollectionError(error)

            result = await topis.run_topis(engine, fetch, links)
            assert result["status"] == "paused_provider_limit" and calls == links[:1]
            assert result["completed"] == 1 and result["remaining"] == 29
            async with factory() as s:
                assert (await s.get(RoadTrafficLatest, ("topis_traffic", links[0]))).speed_kmh == 12
                cycle = await s.get(CollectionRun, UUID(result["run_id"]))
                assert cycle.error_code == error and cycle.completeness == "partial"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_start_record_failure_prevents_http_requests(normalized_database, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(CollectionRepository, "start", fail)

    async def run():
        engine = create_async_engine(normalized_database)
        calls = []

        async def fetch(link):
            calls.append(link)

        try:
            with pytest.raises(RuntimeError, match="database unavailable"):
                await topis.run_topis(engine, fetch, ["1000000000"])
            assert calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
