"""TOPIS rotation, committed progress, concurrency and provider-stop behavior."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import CollectionRun, RawPayload, RoadTrafficLatest
from app.modules.data_pipeline.pipelines import snapshots, topis
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.collection import CollectionRepository
from app.storage.payloads import configured_store

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
                    session, "topis:" + links[0], {"rows": [{"link_id": links[0], "prcs_spd": "0"}]}
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

            calls, reports = [], []
            parent = asyncio.current_task()

            async def fetch(link):
                # Selection must release its connection before collection starts.
                assert parent not in owners.values()
                assert asyncio.current_task() not in owners.values()
                calls.append(link)
                return {"rows": [{"link_id": link, "prcs_spd": "10"}]}

            result = await topis.run_topis_network(engine, fetch, 2, True, report=reports.append)
            assert calls == [links[3], links[0]]
            assert result["status"] == "success", result
            assert reports[0]["scope"] == "scheduled_official_batch"
            assert reports[0]["selected_links"] == 2
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
                    {"rows": [{"link_id": links[0], "prcs_spd": "80"}]},
                    received_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                assert stale.status == "success"
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
            calls, events = [], []
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
                    return {"rows": [{"link_id": link, "prcs_spd": "0"}]}
                finally:
                    active -= 1

            result = await topis.run_topis(engine, fetch, links + links[:2], report=events.append)
            assert peak == 8 and active == 0
            assert calls == links and len(events) == 55
            assert result == {
                "cycle_complete": True,
                "target": 55,
                "status": "failed",
                "counts": {
                    "success": 52,
                    "provider_no_observation": 1,
                    "provider_http_error": 1,
                    "missing_road_geometry": 1,
                },
            }
            async with factory() as session:
                _, stored = await stored_payload(
                    session, "topis_traffic", "report:topis_collection_status"
                )
                assert stored["status"] == "complete"
                for key, value in {
                    "target": 55,
                    "completed": 55,
                    "success": 52,
                    "partial": 1,
                    "no_observation": 1,
                    "failed": 1,
                    "remaining": 0,
                }.items():
                    assert stored[key] == value
                assert stored["error_counts"] == result["counts"]
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.target_key.like("link:%"))
                    )
                )
                assert len(runs) == 55  # One attempt per unique link, not a second success attempt.
                assert (
                    await session.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 52
                )
                observed = await session.get(RoadTrafficLatest, ("topis_traffic", links[0]))
                assert observed.speed_kmh == 0
                payloads = list(
                    await session.scalars(
                        select(RawPayload)
                        .join(CollectionRun)
                        .where(CollectionRun.target_key == "report:topis_collection_status")
                        .order_by(RawPayload.stored_at)
                    )
                )
                assert [configured_store().load_json(p)["completed"] for p in payloads] == [
                    0,
                    50,
                    55,
                ]
                assert all(p.payload_kind == "processing_report" for p in payloads)
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
                    {"rows": [{"link_id": links[0], "prcs_spd": "12"}]},
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
                attempt = await CollectionRepository(session).latest_run(
                    "topis_traffic", "link:" + links[0]
                )
                assert attempt.status == "failed" and attempt.error_code == error
                _, status = await stored_payload(
                    session, "topis_traffic", "report:topis_collection_status"
                )
                assert status["status"] == "paused_provider_limit"
                assert status["completed"] == 1 and status["remaining"] == 29
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_initial_report_failure_is_committed_and_prevents_http_calls(
    normalized_database, monkeypatch
):
    original = snapshots.normalize_snapshot

    def fail_report(session, key, *args, **kwargs):
        if key == "topis_collection_status":
            raise ValueError("report processing failed")
        return original(session, key, *args, **kwargs)

    monkeypatch.setattr(snapshots, "normalize_snapshot", fail_report)

    async def run():
        engine = create_async_engine(normalized_database)
        calls = []

        async def fetch(link):
            calls.append(link)

        try:
            with pytest.raises(CollectionError, match="normalization_failed"):
                await topis.run_topis(engine, fetch, ["1000000000"])
            assert calls == []
            async with async_sessionmaker(engine)() as session:
                runs = list(await session.scalars(select(CollectionRun)))
                assert len(runs) == 1 and runs[0].status == "failed"
                assert await session.scalar(select(func.count()).select_from(RawPayload)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
