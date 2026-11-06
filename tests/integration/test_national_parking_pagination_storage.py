import asyncio

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import (
    CollectionCursorPage,
    CollectionRejectionReason,
    CollectionRun,
    ParkingSourceRecord,
)
from app.modules.data_pipeline.collectors.national_parking import METHODS, collect_national_parking
from app.modules.data_pipeline.pipelines.national_parking import save_national_parking
from app.repositories.national_parking_progress import load_national_parking_progress
from app.repositories.normalization import NormalizedWriter
from tests.integration.test_national_parking_storage import facility, live, operation

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


async def batch(factory, *, fail=None, invalid_names=False, empty=False):
    async with factory() as session:
        progress = await load_national_parking_progress(session)
    calls = []

    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        page = int(request.url.params["pageNo"])
        calls.append((method, page))
        if (method, page) == fail:
            return httpx.Response(503)
        if method == METHODS[2]:
            rows, total = [live("1")], 1
        else:
            maker = facility if method == METHODS[0] else operation
            rows = [maker(str(i)) for i in range((page - 1) * 2 + 1, min(page * 2, 6) + 1)]
            total = 6
            if empty:
                rows = []
            if invalid_names and method == METHODS[0]:
                rows = [{**r, "prk_plce_nm": ""} for r in rows]
        return httpx.Response(200, json={"resultCode": "0", "totalCount": total, method: rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        data = await collect_national_parking(
            http, Settings(_env_file=None), progress=progress, page_size=2, max_pages=2
        )
    return data, calls


async def persist(factory, data):
    async with factory.begin() as session:
        return await save_national_parking(session, data)


async def progress(factory):
    async with factory() as session:
        return await load_national_parking_progress(session)


def test_committed_batches_resume_after_restart_and_old_replay_does_not_rewind(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            first, calls = await batch(factory)
            await persist(factory, first)
            assert (await progress(factory))[METHODS[0]]["next_page"] == 3
            async with factory.begin() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(CollectionCursorPage))
                    == 4
                )
                await session.execute(delete(CollectionRejectionReason))
                await session.execute(delete(CollectionRun))
            second, calls = await batch(factory)
            assert calls == [(METHODS[0], 3), (METHODS[1], 3), (METHODS[2], 1)]
            report = await persist(factory, second)
            assert report["datasets"][METHODS[0]]["pagination"]["scan_complete"]
            state = await progress(factory)
            assert state[METHODS[0]]["next_page"] == 1
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(CollectionCursorPage))
                    == 0
                )
                receipts = dict(
                    (
                        await session.execute(
                            select(
                                ParkingSourceRecord.source_record_id,
                                ParkingSourceRecord.collected_at,
                            )
                        )
                    ).all()
                )
            replay = await persist(factory, first)
            assert replay["status"] == "failed" and replay["error_code"] == "stale_parking_batch"
            assert replay["pagination_progress"]["results"][METHODS[0]] == "held_stale_batch"
            assert await progress(factory) == state
            async with factory() as session:
                assert (
                    dict(
                        (
                            await session.execute(
                                select(
                                    ParkingSourceRecord.source_record_id,
                                    ParkingSourceRecord.collected_at,
                                )
                            )
                        ).all()
                    )
                    == receipts
                )
            third, calls = await batch(factory)
            assert calls[0] == (METHODS[0], 1)
            await persist(factory, third)
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 6
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_request_is_retried_independently_after_successful_prefix_commits(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            first, _ = await batch(factory, fail=(METHODS[0], 2))
            await persist(factory, first)
            state = await progress(factory)
            assert state[METHODS[0]]["next_page"] == 2
            assert state[METHODS[1]]["next_page"] == 3
            second, calls = await batch(factory)
            assert calls[:2] == [(METHODS[0], 2), (METHODS[0], 3)]
            await persist(factory, second)
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 6
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_storage_failure_and_outer_rollback_never_advance_cursor(normalized_database, monkeypatch):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            data, _ = await batch(factory)
            with monkeypatch.context() as patch:

                def fail(*args, **kwargs):
                    raise RuntimeError("synthetic database failure")

                patch.setattr(NormalizedWriter, "parking", fail)
                result = await persist(factory, data)
            assert result["error_code"] == "normalization_failed"
            assert await progress(factory) == {}
            # A failure after typed writes, while persisting the cursor, rolls back both.
            with monkeypatch.context() as patch:

                async def fail_cursor(*args, **kwargs):
                    raise RuntimeError("synthetic cursor failure")

                patch.setattr(
                    "app.modules.data_pipeline.pipelines.national_parking.save_national_parking_progress",
                    fail_cursor,
                )
                result = await persist(factory, data)
                assert result["error_code"] == "normalization_failed"
            assert await progress(factory) == {}
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 0
                )
            retried, calls = await batch(factory)
            assert calls[0] == (METHODS[0], 1)
            with pytest.raises(RuntimeError, match="outer rollback"):
                async with factory.begin() as s:
                    await save_national_parking(s, retried)
                    raise RuntimeError("outer rollback")
            assert await progress(factory) == {}
            await persist(factory, retried)
            assert (await progress(factory))[METHODS[0]]["next_page"] == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_durably_reported_row_rejections_do_not_stall_the_scan(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            data, _ = await batch(factory, invalid_names=True)
            result = await persist(factory, data)
            assert result["datasets"][METHODS[0]]["rejection_reasons"] == {"missing_identity": 4}
            assert (await progress(factory))[METHODS[0]]["next_page"] == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_two_committed_empty_pages_end_inconsistent_scan_without_full_inventory_claim(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            data, _ = await batch(factory)
            await persist(factory, data)
            first_empty, _ = await batch(factory, empty=True)
            await persist(factory, first_empty)
            assert (await progress(factory))[METHODS[0]]["next_page"] == 3
            second_empty, calls = await batch(factory, empty=True)
            assert calls[0] == (METHODS[0], 3)
            result = await persist(factory, second_empty)
            info = result["datasets"][METHODS[0]]["pagination"]
            assert info["terminal_confirmed"] and not info["scan_complete"]
            assert not result["datasets"][METHODS[0]]["complete"]
            assert (await progress(factory))[METHODS[0]]["next_page"] == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
