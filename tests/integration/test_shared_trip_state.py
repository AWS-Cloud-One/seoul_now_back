"""Trip progress and origin drafts shared by several API processes (real PostgreSQL).

Each "pod" has its own engine, store and provider mocks, like two API Pods behind
one load balancer during a rolling update. Providers are deterministic, never live.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import read_session_factory, write_session_factory
from app.schemas.recommendations import ConditionsPatch
from app.services.origin_context import OriginTransition
from app.services.origin_search import OriginSearchResult
from app.services.postgres_origin_context import PostgresOriginContextStore
from app.services.postgres_progress import PostgresProgressStore
from app.services.recommendation_persistence import RecommendationPersistence
from tests.integration.test_recommendation_execution_storage import candidate
from tests.unit.recommendation.test_progress import ids, next_request, request, setup

pytestmark = pytest.mark.integration


@asynccontextmanager
async def pod(url, **store_options):
    database = create_async_engine(url)
    readers, writers = read_session_factory(database), write_session_factory(database)
    store = PostgresProgressStore(writers, **store_options)
    service, source, _ = setup(store)
    source.search.side_effect = lambda q, o, purpose, page: (
        [candidate(i) for i in range((page - 1) * 15, page * 15)],
        page == 5,
    )
    service.execution.persistence = RecommendationPersistence(readers, writers)
    try:
        yield SimpleNamespace(service=service, source=source, store=store, writers=writers)
    finally:
        await database.dispose()


async def sql(env, statement, **values):
    async with env.writers.begin() as session:
        result = await session.execute(text(statement), values)
        return result.all() if result.returns_rows else None


async def lease(env, trip_id):
    rows = await sql(
        env, "SELECT lock_owner, expired FROM trip_progress WHERE trip_id = :t", t=trip_id
    )
    return rows[0] if rows else None


def test_trip_continues_on_another_process_and_replays_there(normalized_database):
    async def run():
        async with pod(normalized_database) as a, pod(normalized_database) as b:
            req = request()
            first = await a.service.execute(req)
            assert first.failure is None and len(ids(first)) == 3

            # The next request lands on the other pod: exclusions and leftovers survive.
            second = await b.service.execute(next_request(req, first))
            assert second.failure is None
            assert set(ids(first)).isdisjoint(ids(second))
            assert b.source.search.await_count == 0  # served from shared leftovers

            # Retrying the first request on the other pod replays it without providers.
            calls = list(b.source.mock_calls)
            replay = await b.service.execute(deepcopy(req))
            assert replay.response == first.response
            assert b.source.mock_calls == calls

            state = await b.store.peek(req.trip_id)
            # No condition changed, so the version stays 0 while exclusions accumulate.
            assert state.version == 0 and state.excluded == set(ids(first)) | set(ids(second))
            assert await b.store.request_count(req.trip_id) == 2
            assert (await lease(a, req.trip_id)).lock_owner is None

    asyncio.run(run())


def test_same_trip_never_runs_concurrently_across_processes(normalized_database):
    async def run():
        # A 1-second lease forces the heartbeat to keep it alive during a slow request.
        async with (
            pod(normalized_database, lease_seconds=1) as a,
            pod(normalized_database, lease_seconds=1) as b,
        ):
            active, peak, runs = [0], [0], [0]

            def track(env):
                # Measure whole requests (the locked section), not routes inside one request.
                original = env.service.prepare

                async def prepare(*args, **kwargs):
                    active[0] += 1
                    runs[0] += 1
                    peak[0] = max(peak[0], active[0])
                    try:
                        await asyncio.sleep(1.5)  # longer than the lease
                        return await original(*args, **kwargs)
                    finally:
                        active[0] -= 1

                env.service.prepare = prepare

            track(a)
            track(b)
            req = request()
            one, two = await asyncio.gather(
                a.service.execute(req), b.service.execute(deepcopy(req))
            )
            assert one.response == two.response and one.failure is None
            # Ran once; the other pod waited, then replayed the stored answer.
            assert peak[0] == 1 and runs[0] == 1

            # Different requests for one trip also run one after another. Both change
            # conditions from version 0, so whichever runs second is outdated.
            peak[0] = runs[0] = 0
            follow = next_request(req, one)
            follow.conditions_patch = ConditionsPatch(headcount=2)
            other = next_request(req, one)
            other.conditions_patch = ConditionsPatch(headcount=3)
            outcomes = await asyncio.gather(a.service.execute(follow), b.service.execute(other))
            assert peak[0] == 1
            assert sum(o.failure is None for o in outcomes) == 1
            assert {o.failure.code for o in outcomes if o.failure} == {
                "CONTEXT_CONDITIONS_MISMATCH"
            }

    asyncio.run(run())


def test_lease_left_by_a_dead_process_expires_and_live_lease_reports_busy(
    normalized_database,
):
    async def run():
        async with pod(normalized_database) as a, pod(normalized_database, wait_seconds=0.5) as b:
            req = request()
            await sql(
                a,
                "INSERT INTO trip_progress (trip_id, expires_at, lock_owner, lock_until) "
                "VALUES (:t, now() + interval '30 minutes', :o, now() + interval '1 second')",
                t=req.trip_id,
                o=uuid4(),
            )
            recovered = await a.service.execute(req)
            assert recovered.failure is None and len(ids(recovered)) == 3

            # A lease that is still alive makes a short-waiting request fail fast and retryable.
            await sql(
                a,
                "UPDATE trip_progress SET lock_owner = :o, lock_until = now() + interval '1 hour' "
                "WHERE trip_id = :t",
                t=req.trip_id,
                o=uuid4(),
            )
            busy = await b.service.execute(next_request(req, recovered))
            assert busy.failure.code == "SERVICE_UNAVAILABLE" and busy.failure.retryable

    asyncio.run(run())


def test_a_writer_that_lost_its_lease_cannot_overwrite_state(normalized_database):
    async def run():
        async with pod(normalized_database) as a:
            req = request()
            thief = uuid4()

            async def route(*args, **kwargs):
                # Another process took over the trip while this request was still working.
                await sql(
                    a,
                    "UPDATE trip_progress SET lock_owner = :o, lock_until = now() + "
                    "interval '1 hour' WHERE trip_id = :t",
                    t=req.trip_id,
                    o=thief,
                )
                return 600, 1000

            a.source.route.side_effect = route
            outcome = await a.service.execute(req)
            assert outcome.failure is not None and outcome.failure.code == "DATA_UNAVAILABLE"
            rows = await sql(
                a,
                "SELECT state, lock_owner FROM trip_progress WHERE trip_id = :t",
                t=req.trip_id,
            )
            assert rows == [(None, thief)]
            assert await a.store.request_count(req.trip_id) == 0
            # The lease is checked inside the condition commit: nothing was committed.
            assert await condition_version(a, req.trip_id) is None

    asyncio.run(run())


async def condition_version(env, trip_id):
    rows = await sql(
        env,
        "SELECT max(version) FROM chat_condition_versions WHERE trip_id = :t",
        t=trip_id,
    )
    return rows[0][0]


def test_losing_the_lease_mid_change_keeps_conditions_and_trip_state_in_step(
    normalized_database,
):
    """Regression: conditions must never reach version 1 while trip state stays at 0."""

    async def run():
        async with pod(normalized_database) as a, pod(normalized_database) as b:
            req = request()
            first = await a.service.execute(req)
            assert first.failure is None and await condition_version(a, req.trip_id) == 0

            change = next_request(req, first)
            change.conditions_patch = ConditionsPatch(headcount=2)
            thief = uuid4()

            async def route(*args, **kwargs):
                # The lease moves to another process while this change is still computing.
                await sql(
                    a,
                    "UPDATE trip_progress SET lock_owner = :o, lock_until = now() + "
                    "interval '1 hour' WHERE trip_id = :t",
                    t=req.trip_id,
                    o=thief,
                )
                return 600, 1000

            a.source.route.side_effect = route
            lost = await a.service.execute(change)
            assert lost.failure is not None and lost.failure.code == "DATA_UNAVAILABLE"
            assert await condition_version(a, req.trip_id) == 0
            assert (await b.store.peek(req.trip_id)).version == 0

            # Once that lease expires, the same change succeeds on the other pod.
            await sql(
                a,
                "UPDATE trip_progress SET lock_until = now() - interval '1 second' "
                "WHERE trip_id = :t",
                t=req.trip_id,
            )
            retried = change.model_copy(update={"request_id": uuid4()}, deep=True)
            done = await b.service.execute(retried)
            assert done.failure is None and done.response.condition_update.version == 1
            assert await condition_version(b, req.trip_id) == 1
            assert (await b.store.peek(req.trip_id)).version == 1

    asyncio.run(run())


def test_trip_state_is_saved_inside_the_condition_commit(normalized_database):
    """Even if nothing runs after the commit, state and cached answer already match."""

    async def run():
        async with pod(normalized_database) as a:
            req = request()

            async def no_save(*args, **kwargs):
                raise AssertionError("an applied change must not need the post-commit save")

            a.store.save = no_save
            outcome = await a.service.execute(req)
            assert outcome.failure is None
            state = await a.store.peek(req.trip_id)
            assert state.version == 0 and state.excluded == set(ids(outcome))
            assert await a.store.request_count(req.trip_id) == 1
            assert (await lease(a, req.trip_id)).lock_owner is None
            replay = await a.service.execute(deepcopy(req))
            assert replay.response == outcome.response

    asyncio.run(run())


def test_cancelled_request_persists_a_tombstone_and_releases_the_lease(normalized_database):
    async def run():
        async with pod(normalized_database) as a:
            req = request()
            first = await a.service.execute(req)
            entered, never = asyncio.Event(), asyncio.Event()

            async def route(*args, **kwargs):
                entered.set()
                await never.wait()

            a.source.route.side_effect = route
            task = asyncio.create_task(a.service.execute(next_request(req, first)))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            row = await lease(a, req.trip_id)
            assert row.expired is True and row.lock_owner is None
            assert await a.store.request_count(req.trip_id) == 0
            again = await a.service.execute(next_request(req, first))
            assert again.failure.code == "CONTEXT_EXPIRED"

    asyncio.run(run())


def test_expiry_follows_the_database_clock_and_unknown_state_is_expired(normalized_database):
    async def run():
        async with pod(normalized_database) as a, pod(normalized_database) as b:
            req = request()
            first = await a.service.execute(req)
            await sql(
                a,
                "UPDATE trip_progress SET expires_at = now() - interval '1 second' "
                "WHERE trip_id = :t",
                t=req.trip_id,
            )
            late = await b.service.execute(next_request(req, first))
            assert late.failure.code == "CONTEXT_EXPIRED"
            assert (await b.store.peek(req.trip_id)).expired is True

            # State written by an incompatible version is never guessed at.
            other = request()
            done = await a.service.execute(other)
            await sql(
                a,
                "UPDATE trip_progress SET state = jsonb_set(state, '{schema}', '999') "
                "WHERE trip_id = :t",
                t=other.trip_id,
            )
            unknown = await b.service.execute(next_request(other, done))
            assert unknown.failure.code == "CONTEXT_EXPIRED"
            assert (await lease(b, other.trip_id)).expired is True

    asyncio.run(run())


def origin_request():
    req = request()
    return req, SimpleNamespace(
        trip_id=req.trip_id,
        request_id=uuid4(),
        condition_version=0,
        current_conditions=req.current_conditions,
    )


def outcome(applied=False):
    return SimpleNamespace(
        response=SimpleNamespace(condition_update=SimpleNamespace(applied=applied)),
        failure=None,
    )


def test_origin_drafts_are_shared_latest_wins_and_rollback_writes_nothing(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        other = create_async_engine(normalized_database)
        writers = write_session_factory(engine)
        try:
            a = PostgresOriginContextStore(writers)
            b = PostgresOriginContextStore(write_session_factory(other))
            _, current = origin_request()
            result = OriginSearchResult(
                candidates=(),
                unsupported_candidates=(),
                mode="refine",
                search_limited=False,
                warnings=(),
            )
            patch = ConditionsPatch(headcount=None)  # explicit null must survive storage
            draft = await a.reserve(current, result, patch)
            await a.publish(OriginTransition(after=draft), current, outcome())

            shared = await b.resolve(draft.token, current)
            assert shared == draft
            assert shared.patch.model_fields_set == {"headcount"}
            assert shared.expires_at.tzinfo is not None
            assert shared.expires_at > datetime.now(UTC)

            # A newer draft for the trip replaces the latest one on every process.
            newer = await b.reserve(current, result, ConditionsPatch(), previous=shared)
            follow = SimpleNamespace(**{**vars(current), "request_id": uuid4()})
            await b.publish(OriginTransition(before=shared, after=newer), follow, outcome())
            assert (await a.peek(draft.token)).used_by == follow.request_id
            assert (await a.resolve(newer.token, follow)).token == newer.token
            assert newer.expires_at == draft.expires_at  # a follow-up keeps the deadline

            # Published inside a transaction that rolls back: nothing becomes visible.
            third = await a.reserve(current, result, ConditionsPatch())
            with pytest.raises(RuntimeError):
                async with writers.begin() as session:
                    await a.publish(
                        OriginTransition(after=third), current, outcome(applied=True), session
                    )
                    raise RuntimeError("commit failed")
            assert await b.peek(third.token) is None
            assert (await b.resolve(newer.token, follow)).token == newer.token
        finally:
            await engine.dispose()
            await other.dispose()

    asyncio.run(run())
