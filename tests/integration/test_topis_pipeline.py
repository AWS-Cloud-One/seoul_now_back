"""TOPIS rotation, committed progress, concurrency and provider-stop behavior."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import (
    CollectionRun,
    MapRoadObservation,
    RawPayload,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.pipelines import topis
from app.repositories.collection import CollectionRepository
from app.services.map_publication import MapPublicationService
from app.storage.payloads import configured_store
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration


async def seed_roads(factory, links):
    async with factory.begin() as session:
        result = await save_snapshot(
            session,
            "topis_geometry:2025",
            {
                "features": [
                    {
                        "id": link,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [127.01, 37.5]],
                        },
                    }
                    for link in links
                ],
            },
        )
        assert result.status == "success"


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


def test_target_rotation_uses_success_partial_and_failed_attempts(normalized_database, monkeypatch):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(4)]
        try:
            await seed_roads(factory, links)
            async with factory.begin() as session:
                success = await save_snapshot(
                    session,
                    "topis:" + links[0],
                    {"rows": [{"link_id": links[0], "prcs_spd": "0", "prcs_trv_time": "0"}]},
                )
                failed = await save_snapshot(
                    session,
                    "topis_attempt:" + links[1],
                    {"status": "failed", "error_code": "provider_http_error"},
                )
                partial = await save_snapshot(session, "topis:" + links[2], {"rows": []})
                assert [success.status, failed.status, partial.status] == [
                    "success",
                    "failed",
                    "partial",
                ]
                for index, result in enumerate((success, failed, partial)):
                    row = await session.get(CollectionRun, result.run_id)
                    row.started_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
                    row.finished_at = row.started_at + timedelta(seconds=1)

            def read(*args):
                pytest.fail("TOPIS target selection must not read citydata or other payload files")

            monkeypatch.setattr(type(configured_store()), "load_json", read)
            owners = watch_transactions(engine)
            selected, report = await topis.targets(engine, 2, True)
            assert selected == [links[3], links[0]]
            assert report["total_links"] == 4 and report["truncated"]
            assert not owners
            all_links, _ = await topis.targets(engine, 1)
            assert all_links == links  # Explicit --all remains unbounded by max-links.

            calls = []
            parent = asyncio.current_task()

            async def fetch(link):
                # Selection must release its connection before collection starts.
                assert parent not in owners.values()
                assert asyncio.current_task() not in owners.values()
                calls.append(link)
                return {"rows": [{"link_id": link, "prcs_spd": "10", "prcs_trv_time": "30"}]}

            result = await topis.run_topis_network(engine, fetch, 2, True)
            assert calls == [links[3], links[0]]
            assert result["status"] == "success", result
            assert result["selection"]["scope"] == "scheduled_official_batch"
            assert result["selection"]["selected_links"] == 2
            assert not owners
            async with factory() as session:
                for link in calls:
                    latest = await session.get(RoadTrafficLatest, ("topis_traffic", link))
                    assert latest.speed_kmh == 10
                updated_payload = latest.payload_id
            # With multiple roads present, compare against this road's payload only.
            async with factory.begin() as session:
                stale = await save_snapshot(
                    session,
                    "topis:" + links[0],
                    {"rows": [{"link_id": links[0], "prcs_spd": "80", "prcs_trv_time": "10"}]},
                    received_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                assert stale.status == "partial" and stale.error_code == "traffic_time_guard"
            async with factory() as session:
                latest = await session.get(RoadTrafficLatest, ("topis_traffic", links[0]))
                assert latest.speed_kmh == 10 and latest.payload_id == updated_payload
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parallel_cycle_preserves_progress_outcomes_and_deduplicates(
    normalized_database, monkeypatch, stored_payload
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(55)]
        try:
            await seed_roads(factory, [link for link in links if link != links[3]])
            owners = watch_transactions(engine)
            calls = []
            active, peak = 0, 0
            first_wave = asyncio.Event()

            async def fetch(link):
                nonlocal active, peak
                assert asyncio.current_task() not in owners.values()
                calls.append(link)
                active += 1
                peak = max(active, peak)
                if active == 8:
                    first_wave.set()
                try:
                    await asyncio.wait_for(first_wave.wait(), 5)
                    if link == links[1]:
                        raise CollectionError("provider_no_observation")
                    if link == links[2]:
                        raise CollectionError("provider_http_error")
                    return {"rows": [{"link_id": link, "prcs_spd": "0", "prcs_trv_time": "0"}]}
                finally:
                    active -= 1

            result = await topis.run_topis(engine, fetch, links + links[:2])
            assert peak == 8 and active == 0
            assert calls == links
            assert result.pop("publication")["status"] == "success"
            assert {
                key: result[key]
                for key in ("cycle_complete", "target", "status", "retry_attempts", "counts")
            } == {
                "cycle_complete": True,
                "target": 55,
                "status": "partial",
                "retry_attempts": 0,
                "counts": {
                    "success": 52,
                    "provider_no_observation": 1,
                    "provider_http_error": 1,
                    "missing_road_geometry": 1,
                },
            }
            async with factory() as session:
                payload, stored = await stored_payload(session, "topis_traffic", "network")
                assert stored["summary"]["completed"] == 55
                assert stored["summary"]["remaining"] == 0
                assert stored["summary"]["counts"] == result["counts"]
                assert len(stored["entries"]) == 55
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.source == "topis_traffic")
                    )
                )
                assert len(runs) == 1 and runs[0].target_key == "network"
                assert (runs[0].received, runs[0].accepted, runs[0].rejected) == (55, 52, 3)
                assert (
                    await session.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 52
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RoadTrafficAttemptLatest))
                    == 54
                )
                observed = await session.get(RoadTrafficLatest, ("topis_traffic", links[0]))
                assert observed.speed_kmh == 0 and observed.payload_id == payload.payload_id
                payloads = list(
                    await session.scalars(
                        select(RawPayload).where(RawPayload.source == "topis_traffic")
                    )
                )
                assert len(payloads) == 1 and payloads[0].part_key == "topis:cycle"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retries_only_transient_failed_links_and_preserves_exhausted_link_value(
    normalized_database, monkeypatch, stored_payload
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "RETRY_DELAYS", (0, 0))

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(4)]
        calls = Counter()
        try:
            await seed_roads(factory, links)
            async with factory.begin() as session:
                previous = await save_snapshot(
                    session,
                    "topis:" + links[2],
                    {"rows": [{"link_id": links[2], "prcs_spd": "12", "prcs_trv_time": "40"}]},
                    received_at=datetime.now(UTC) - timedelta(minutes=1),
                )
                await MapPublicationService(session).publish("roads")
            owners = watch_transactions(engine)

            async def handler(request):
                assert asyncio.current_task() not in owners.values()
                link = request.url.path.rsplit("/", 1)[1]
                calls[link] += 1
                if link == links[1] and calls[link] == 1:
                    return httpx.Response(503)
                if link == links[2] or link == links[1] and calls[link] == 2:
                    raise httpx.ReadTimeout("private provider URL", request=request)
                if link == links[3]:
                    return httpx.Response(200, text="<RESULT><CODE>INFO-200</CODE></RESULT>")
                return httpx.Response(
                    200,
                    text=f"<TrafficInfo><RESULT><CODE>INFO-000</CODE></RESULT><row>"
                    f"<link_id>{link}</link_id><prcs_spd>25</prcs_spd>"
                    "<prcs_trv_time>30</prcs_trv_time></row></TrafficInfo>",
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await topis.run_topis(
                    engine,
                    lambda link: collect_topis(client, "test-key", link),
                    links,
                )
            assert dict(calls) == dict(zip(links, (1, 3, 3, 1), strict=True))
            assert result["status"] == "partial" and result["cycle_complete"]
            assert result["retry_attempts"] == 4
            assert result["counts"] == {
                "success": 2,
                "provider_network_error": 1,
                "provider_no_observation": 1,
            }
            assert result["publication"]["status"] == "success"
            async with factory() as session:
                for link in links[:2]:
                    value = await session.get(RoadTrafficLatest, ("topis_traffic", link))
                    assert value.speed_kmh == 25
                    state = await session.get(RoadTrafficAttemptLatest, link)
                    assert state.status == "success" and state.attempts == calls[link]
                old = await session.get(RoadTrafficLatest, ("topis_traffic", links[2]))
                assert old.payload_id == previous.payload_id and old.speed_kmh == 12
                published = await session.get(MapRoadObservation, links[2])
                assert published.collection_failed and published.payload_id == previous.payload_id
                assert published.speed_kmh == 12
                attempt = await session.get(RoadTrafficAttemptLatest, links[2])
                assert attempt.status == "failed" and attempt.error_code == "provider_network_error"
                _, progress = await stored_payload(session, "topis_traffic", "network")
                assert {e["link_id"]: e["attempts"] for e in progress["entries"]} == dict(calls)
                progress = progress["summary"]
                assert progress["completed"] == 4 and progress["remaining"] == 0
                assert (
                    progress["retry_attempts"] == 4
                    and progress["counts"]["provider_network_error"] == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "error", ["provider_quota_exceeded", "provider_auth_error", "missing_topis_api_key"]
)
def test_provider_stop_prevents_new_requests_and_keeps_last_good_value(
    normalized_database, monkeypatch, error, stored_payload
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        links = [str(1000000000 + i) for i in range(30)]
        try:
            await seed_roads(factory, links[:1])
            async with factory.begin() as session:
                previous = await save_snapshot(
                    session,
                    "topis:" + links[0],
                    {"rows": [{"link_id": links[0], "prcs_spd": "12", "prcs_trv_time": "40"}]},
                )
                assert previous.status == "success"
            calls = []

            async def fetch(link):
                calls.append(link)
                raise CollectionError(error)

            result = await topis.run_topis(engine, fetch, links)
            assert result["status"] == "paused_provider_limit" and not result["cycle_complete"]
            assert calls == links[:1] and result["counts"] == {error: 1}
            async with factory() as session:
                observed = await session.get(RoadTrafficLatest, ("topis_traffic", links[0]))
                assert observed.payload_id == previous.payload_id and observed.speed_kmh == 12
                attempt = await session.get(RoadTrafficAttemptLatest, links[0])
                assert attempt.status == "failed" and attempt.error_code == error
                _, status = await stored_payload(session, "topis_traffic", "network")
                status = status["summary"]
                assert status["status"] == "paused_provider_limit"
                assert status["completed"] == 1 and status["remaining"] == 29
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_initial_checkpoint_failure_prevents_http_calls(normalized_database, monkeypatch):
    async def fail_archive(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(CollectionRepository, "archive", fail_archive)

    async def run():
        engine = create_async_engine(normalized_database)
        calls = []

        async def fetch(link):
            calls.append(link)

        try:
            with pytest.raises(OSError, match="storage unavailable"):
                await topis.run_topis(engine, fetch, ["1000000000"])
            assert calls == []
            async with async_sessionmaker(engine)() as session:
                assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 0
                assert await session.scalar(select(func.count()).select_from(RawPayload)) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
