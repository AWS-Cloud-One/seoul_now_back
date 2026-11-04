"""Cycle bounds must not delete current values, shared evidence or recoverable progress."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_topis_pipeline import seed_roads

from app.clients.seoul import CollectionError
from app.models import (
    CollectionRun,
    LegacyArchive,
    MapRoadObservation,
    PayloadFileDeletion,
    RawPayload,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.pipelines import topis, topis_storage
from app.repositories.collection import CollectionRepository
from app.repositories.topis import prune_history
from app.services.map_publication import MapPublicationService
from app.storage.payloads import PayloadStore, configured_store

pytestmark = pytest.mark.integration
LINKS = ["1000000000", "1000000001", "1000000002"]


def response(link, speed=25):
    return {"rows": [{"link_id": link, "prcs_spd": str(speed), "prcs_trv_time": "30"}]}


def test_twenty_cycles_preserve_old_last_good_and_current_attempt_state(
    normalized_database, monkeypatch
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_roads(factory, LINKS)

            async def good(link):
                return response(link)

            first = await topis.run_topis(engine, good, LINKS)
            async with factory() as s:
                value = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1]))
                old_id, receipt = value.payload_id, value.received_at
                old_file = (await s.get(RawPayload, old_id)).file_ref
            second = await topis.run_topis(engine, good, LINKS[:1])
            async with factory() as s:
                removed_file = (
                    await s.scalar(
                        select(RawPayload).where(RawPayload.run_id == UUID(second["run_id"]))
                    )
                ).file_ref

            async def mixed(link):
                if link == LINKS[1]:
                    raise CollectionError("provider_http_error")
                return response(link, 40)

            for _ in range(20):
                result = await topis.run_topis(engine, mixed, LINKS[:2])
                assert result["status"] == "partial"
            async with factory() as s:
                runs = list(
                    await s.scalars(
                        select(CollectionRun).where(CollectionRun.source == "topis_traffic")
                    )
                )
                assert len(runs) == 20 and all(r.target_key == "network" for r in runs)
                assert await s.get(CollectionRun, UUID(first["run_id"])) is None
                assert await s.get(CollectionRun, UUID(second["run_id"])) is None
                assert (
                    await s.scalar(select(func.count()).select_from(RoadTrafficAttemptLatest)) == 3
                )
                assert await s.scalar(select(func.count()).select_from(PayloadFileDeletion)) == 0
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .where(RawPayload.source == "topis_traffic")
                    )
                    == 21
                )
                raw = await s.get(RawPayload, old_id)
                assert raw.run_id is None and raw.source == "topis_traffic"
                assert configured_store().load_json(raw)["entries"]
                unchanged = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[1]))
                assert unchanged.payload_id == old_id and unchanged.received_at == receipt
                published = await s.get(MapRoadObservation, LINKS[1])
                assert published.collection_failed and published.speed_kmh == 25
                assert published.received_at == receipt and published.payload_id == old_id
                untouched = await s.get(RoadTrafficAttemptLatest, LINKS[2])
                assert untouched.status == "success" and untouched.run_id is None
                referenced_files = set(await s.scalars(select(RawPayload.file_ref)))
                referenced_files.update(await s.scalars(select(LegacyArchive.file_ref)))
                actual_files = {
                    str(path.relative_to(configured_store().root))
                    for path in configured_store().root.glob("sha256/*/*.blob")
                }
                assert actual_files == referenced_files
            assert (configured_store().root / old_file).exists()
            assert not (configured_store().root / removed_file).exists()
            async with factory.begin() as s:
                await MapPublicationService(s).publish("roads")
            async with factory() as s:
                assert (await s.get(MapRoadObservation, LINKS[2])).speed_kmh == 25
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cleanup_queue_is_transactional_retryable_and_protects_shared_ancestry(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as s:
                repo = CollectionRepository(s)
                old = await repo.start("topis_traffic", "link:old")
                shared = await repo.archive(old.id, {"shared": True}, None)
                disposable = await repo.archive(old.id, {"remove": True}, None, part="other")
                disposable_child = await repo.archive(
                    old.id, {"remove_child": True}, None, part="disposable_child"
                )
                disposable_child.parent_payload_id = disposable.payload_id
                parent = await repo.archive(old.id, {"parent": True}, None, part="parent")
                await repo.finish(old, status="success")
                other = await repo.start("seoul_citydata", "unrelated")
                copy = await repo.archive(other.id, {"shared": True}, None)
                child = await repo.archive(other.id, {"child": True}, None, part="child")
                child.parent_payload_id = parent.payload_id
                child.source = None  # Historical nullable metadata must still protect ancestors.
                await repo.finish(other, status="success")
                assert copy.file_ref == shared.file_ref
            with pytest.raises(RuntimeError):
                async with factory.begin() as s:
                    await prune_history(s)
                    raise RuntimeError("rollback")
            assert (configured_store().root / disposable.file_ref).exists()
            async with factory() as s:
                assert await s.get(RawPayload, disposable.payload_id) is not None
                assert await s.scalar(select(func.count()).select_from(PayloadFileDeletion)) == 0
            original = PayloadStore.delete

            def fail_delete(*args):
                raise OSError("temporary filesystem failure")

            monkeypatch.setattr(PayloadStore, "delete", fail_delete)
            await topis_storage.maintain_cycles(engine)
            async with factory() as s:
                assert await s.get(RawPayload, disposable.payload_id) is None
                assert await s.get(CollectionRun, other.id) is not None
                assert await s.get(PayloadFileDeletion, disposable.file_ref) is not None
                assert await s.get(PayloadFileDeletion, shared.file_ref) is None
                assert (await s.get(RawPayload, parent.payload_id)).run_id is None
            monkeypatch.setattr(PayloadStore, "delete", original)
            await topis_storage.maintain_cycles(engine)
            assert not (configured_store().root / disposable.file_ref).exists()
            assert (configured_store().root / shared.file_ref).exists()
            assert (configured_store().root / parent.file_ref).exists()
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(PayloadFileDeletion)) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_interrupted_cycle_recovers_committed_checkpoint_and_real_receipt(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        receipt = datetime.now(UTC) - timedelta(minutes=10)
        try:
            await seed_roads(factory, LINKS[:1])
            async with factory.begin() as s:
                repo = CollectionRepository(s)
                cycle = await repo.start("topis_traffic", "network")
                await repo.archive(
                    cycle.id, {"target": 3}, None, kind="derived_snapshot", part="topis:start"
                )
                await topis_storage.checkpoint(
                    s,
                    cycle.id,
                    [
                        {
                            "link_id": LINKS[0],
                            "data": response(LINKS[0]),
                            "received_at": receipt,
                            "attempts": 1,
                            "error_code": None,
                        }
                    ],
                    0,
                )
            result = await topis_storage.maintain_cycles(engine, recover=True)
            assert result["status"] == "success"
            async with factory.begin() as s:
                await MapPublicationService(s).publish("roads")
            async with factory() as s:
                cycle = await s.get(CollectionRun, cycle.id)
                assert cycle.status == "partial" and cycle.error_code == "collection_interrupted"
                payloads = list(
                    await s.scalars(select(RawPayload).where(RawPayload.run_id == cycle.id))
                )
                assert len(payloads) == 1 and payloads[0].part_key == "topis:cycle"
                document = configured_store().load_json(payloads[0])
                assert (
                    document["summary"]["completed"] == 1 and document["summary"]["remaining"] == 2
                )
                assert document["entries"][0]["processing"]["updated"]
                value = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0]))
                assert value.payload_id == payloads[0].payload_id and value.received_at == receipt
                view = await s.get(MapRoadObservation, LINKS[0])
                assert view.speed_kmh == 25 and view.received_at == receipt
                assert not view.collection_failed
                assert await s.scalar(select(func.count()).select_from(PayloadFileDeletion)) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unknown_legacy_receipt_is_not_replaced_by_cycle_start(normalized_database, monkeypatch):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, LINKS[:1])

            async def good(link):
                return response(link)

            await topis.run_topis(engine, good, LINKS[:1])
            async with factory.begin() as s:
                value = await s.get(RoadTrafficLatest, ("topis_traffic", LINKS[0]))
                value.received_at = None
                await s.flush()
                await MapPublicationService(s).publish("roads")
            async with factory() as s:
                view = await s.get(MapRoadObservation, LINKS[0])
                assert view.speed_kmh == 25 and view.received_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


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
