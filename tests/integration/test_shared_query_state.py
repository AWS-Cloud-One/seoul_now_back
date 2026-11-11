"""Cursors and P03 replays survive across API processes and restarts (real PostgreSQL)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import ServiceError
from app.db.session import write_session_factory
from app.schemas.location import Coordinates
from app.schemas.query_services import FoundPlace
from app.services.query_cursors import PostgresQueryCursorStore
from app.services.read_replay import PostgresReadReplay

pytestmark = pytest.mark.integration


class Reading(BaseModel):
    value: int
    at: datetime


def engine_for(url):
    return create_async_engine(url)


def place(index):
    return FoundPlace(
        place_id=f"kakao:{index}",
        name=f"장소{index}",
        category_code=None,
        category_name=None,
        coordinates=Coordinates(latitude=37.5, longitude=127.0),
        address=None,
        road_address=None,
        phone=None,
        provider_url=None,
        distance_meters=Decimal("1.5").__float__(),
        collected_at=datetime(2026, 11, 11, tzinfo=UTC),
    )


async def expire(engine, table, column="expires_at"):
    async with engine.begin() as connection:
        await connection.execute(text(f"UPDATE {table} SET {column} = now() - interval '1 second'"))


def test_cursor_is_shared_between_two_processes_and_keeps_binding(normalized_database):
    async def run():
        engine = engine_for(normalized_database)
        try:
            # Two stores on separate engines model two API pods sharing one database.
            other = engine_for(normalized_database)
            first = PostgresQueryCursorStore(write_session_factory(engine))
            second = PostgresQueryCursorStore(write_session_factory(other))
            scope = ("places", "카페", 10, {"latitude": 37.5, "longitude": 127.0}, 800, "CE7")
            state = {"page": 2, "buffer": [place(1), {"raw": "row"}], "ended": False}
            token = await first.issue(scope, state)
            assert len(token) == 48

            restored = await second.read(token, scope)
            assert restored["page"] == 2 and restored["ended"] is False
            assert restored["buffer"][0]["place_id"] == "kakao:1"
            assert restored["buffer"][1] == {"raw": "row"}
            # The same cursor can be read again, and a different binding is rejected.
            assert (await first.read(token, scope))["page"] == 2
            with pytest.raises(ServiceError) as error:
                await second.read(token, ("places", "다른 검색어", 10, None, 800, "CE7"))
            assert error.value.code == "INVALID_CURSOR"
            with pytest.raises(ServiceError) as error:
                await second.read("z" * 48, scope)
            assert error.value.code == "INVALID_CURSOR"
            with pytest.raises(ServiceError) as error:
                await second.read("a" * 48, scope)
            assert error.value.code == "CURSOR_EXPIRED"
            await other.dispose()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cursor_expiry_capacity_and_cleanup(normalized_database):
    async def run():
        engine = engine_for(normalized_database)
        try:
            store = PostgresQueryCursorStore(write_session_factory(engine), capacity=2)
            first = await store.issue("scope", {"n": 1})
            await store.issue("scope", {"n": 2})
            with pytest.raises(ServiceError) as error:
                await store.issue("scope", {"n": 3})
            assert error.value.code == "SERVICE_UNAVAILABLE"
            await expire(engine, "query_cursors")
            with pytest.raises(ServiceError) as error:
                await store.read(first, "scope")
            assert error.value.code == "CURSOR_EXPIRED"
            # Issuing removes expired rows, which frees capacity.
            assert await store.issue("scope", {"n": 4})
            async with engine.connect() as connection:
                count = (await connection.execute(text("SELECT count(*) FROM query_cursors"))).one()
            assert count[0] == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_replay_returns_stored_value_on_another_process_without_recomputing(normalized_database):
    async def run():
        engine, other = engine_for(normalized_database), engine_for(normalized_database)
        try:
            first = PostgresReadReplay(write_session_factory(engine))
            second = PostgresReadReplay(write_session_factory(other))
            key = (uuid4(), uuid4())
            calls = []

            async def compute():
                calls.append(1)
                return Reading(value=7, at=datetime(2026, 11, 11, 1, 2, 3, tzinfo=UTC))

            one = await first.run(key, "sig", compute, model=Reading)
            two = await second.run(key, "sig", compute, model=Reading)
            assert one == two and one.value == 7 and len(calls) == 1
            with pytest.raises(ServiceError) as error:
                await second.run(key, "different", compute, model=Reading)
            assert error.value.code == "REQUEST_ID_CONFLICT"
            assert len(calls) == 1
        finally:
            await engine.dispose()
            await other.dispose()

    asyncio.run(run())


def test_replay_failure_is_replayed_with_retry_metadata_and_expiry_allows_retry(
    normalized_database,
):
    async def run():
        engine, other = engine_for(normalized_database), engine_for(normalized_database)
        try:
            first = PostgresReadReplay(write_session_factory(engine))
            second = PostgresReadReplay(write_session_factory(other))
            key = (uuid4(), uuid4())
            calls = []

            async def failing():
                calls.append(1)
                raise ServiceError("RATE_LIMITED", "잠시 후 재시도", retry_after_seconds=30)

            with pytest.raises(ServiceError) as error:
                await first.run(key, "sig", failing, model=Reading)
            assert error.value.code == "RATE_LIMITED"
            with pytest.raises(ServiceError) as replayed:
                await second.run(key, "sig", failing, model=Reading)
            assert replayed.value.code == "RATE_LIMITED"
            assert replayed.value.retry_after_seconds == 30 and len(calls) == 1

            # After expiry the same request ID computes again and can succeed.
            await expire(engine, "read_replays")

            async def succeeding():
                return Reading(value=1, at=datetime(2026, 11, 11, tzinfo=UTC))

            assert (await second.run(key, "sig", succeeding, model=Reading)).value == 1
        finally:
            await engine.dispose()
            await other.dispose()

    asyncio.run(run())


def test_concurrent_identical_requests_share_one_computation_and_one_answer(normalized_database):
    async def run():
        engine, other = engine_for(normalized_database), engine_for(normalized_database)
        try:
            first = PostgresReadReplay(write_session_factory(engine))
            second = PostgresReadReplay(write_session_factory(other))
            key = (uuid4(), uuid4())
            release, calls = asyncio.Event(), []

            def computing(label):
                async def compute():
                    calls.append(label)
                    await release.wait()
                    return Reading(value=len(calls), at=datetime(2026, 11, 11, tzinfo=UTC))

                return compute

            # Same process: waiters join the running computation.
            a = asyncio.create_task(first.run(key, "sig", computing("a"), model=Reading))
            b = asyncio.create_task(first.run(key, "sig", computing("b"), model=Reading))
            await asyncio.sleep(0.2)
            release.set()
            one, two = await asyncio.gather(a, b)
            assert one == two and calls == ["a"]

            # Different processes racing: both may compute, both return the stored winner.
            key = (uuid4(), uuid4())
            calls.clear()
            release.clear()
            c = asyncio.create_task(first.run(key, "sig", computing("c"), model=Reading))
            d = asyncio.create_task(second.run(key, "sig", computing("d"), model=Reading))
            await asyncio.sleep(0.3)
            release.set()
            three, four = await asyncio.gather(c, d)
            assert three == four
        finally:
            await engine.dispose()
            await other.dispose()

    asyncio.run(run())


def test_a_late_failure_returns_the_success_another_process_stored_first(normalized_database):
    """Regression: one request ID must not succeed on one Pod and fail on another."""

    async def run():
        engine, other = engine_for(normalized_database), engine_for(normalized_database)
        try:
            first = PostgresReadReplay(write_session_factory(engine))
            second = PostgresReadReplay(write_session_factory(other))
            key = (uuid4(), uuid4())
            started, give_up = asyncio.Event(), asyncio.Event()

            async def slow_then_timeout():
                started.set()
                await give_up.wait()
                raise ServiceError("PROVIDER_TIMEOUT", "이동시간 확인 시간 초과")

            async def success():
                return Reading(value=9, at=datetime(2026, 11, 11, tzinfo=UTC))

            # Pod B starts first and is still waiting on the provider...
            late = asyncio.create_task(second.run(key, "sig", slow_then_timeout, model=Reading))
            await asyncio.wait_for(started.wait(), 5)
            # ...while pod A answers and stores the success.
            won = await first.run(key, "sig", success, model=Reading)
            give_up.set()
            assert (await late) == won and won.value == 9

            # The reverse also holds: a stored failure wins over a later success.
            key = (uuid4(), uuid4())
            started.clear()
            give_up.clear()

            async def failing():
                raise ServiceError("RATE_LIMITED", "잠시 후 재시도", retry_after_seconds=5)

            async def slow_success():
                started.set()
                await give_up.wait()
                return Reading(value=1, at=datetime(2026, 11, 11, tzinfo=UTC))

            later = asyncio.create_task(second.run(key, "sig", slow_success, model=Reading))
            await asyncio.wait_for(started.wait(), 5)
            with pytest.raises(ServiceError) as stored:
                await first.run(key, "sig", failing, model=Reading)
            give_up.set()
            with pytest.raises(ServiceError) as replayed:
                await later
            assert replayed.value.code == stored.value.code == "RATE_LIMITED"
            assert replayed.value.retry_after_seconds == 5
        finally:
            await engine.dispose()
            await other.dispose()

    asyncio.run(run())


def test_replay_capacity_is_enforced(normalized_database):
    async def run():
        engine = engine_for(normalized_database)
        try:
            replay = PostgresReadReplay(write_session_factory(engine), capacity=1)

            async def compute():
                return Reading(value=1, at=datetime(2026, 11, 11, tzinfo=UTC))

            await replay.run((uuid4(), uuid4()), "sig", compute, model=Reading)
            with pytest.raises(ServiceError) as error:
                await replay.run((uuid4(), uuid4()), "sig", compute, model=Reading)
            assert error.value.code == "SERVICE_UNAVAILABLE"
        finally:
            await engine.dispose()

    asyncio.run(run())
