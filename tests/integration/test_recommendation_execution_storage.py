"""Real commit/replay boundaries; providers are deterministic, never paid/live."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import ServiceError
from app.db.session import read_session_factory, write_session_factory
from app.models import ChatConditionVersion, ChatSession, RecommendationResult
from app.modules.recommendation.progress import MemoryProgressStore
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_adapters import RecommendationConditionsValidator
from app.services.recommendation_conditions import RecommendationConditionsService
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendation_persistence import RecommendationPersistence
from tests.unit.recommendation.test_progress import candidate as search_candidate
from tests.unit.recommendation.test_progress import ids, next_request, request, setup

pytestmark = pytest.mark.integration


def candidate(index):
    value = search_candidate(index)
    value.place.place_id = "kakao:" + value.place.place_id
    return value


@asynccontextmanager
async def runtime(url, *, store=None, ttl=1800):
    database = create_async_engine(url)
    readers, writers = read_session_factory(database), write_session_factory(database)
    service, source, engine = setup(store)
    source.search.side_effect = lambda q, o, purpose, page: (
        [candidate(i) for i in range((page - 1) * 15, page * 15)],
        page == 5,
    )
    persistence = RecommendationPersistence(readers, writers, context_ttl_seconds=ttl)
    service.execution.persistence = persistence
    try:
        yield SimpleNamespace(
            service=service,
            source=source,
            engine=engine,
            store=service.execution.store,
            persistence=persistence,
            readers=readers,
            writers=writers,
        )
    finally:
        await database.dispose()


class WriteHook:
    """Run a test hook after all writes but before the real transaction commits."""

    def __init__(self, factory, hook):
        self.factory, self.hook = factory, hook

    @asynccontextmanager
    async def begin(self):
        async with self.factory.begin() as session:
            yield session
            await self.hook(session)


async def counts(env):
    async with env.readers() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (ChatSession, ChatConditionVersion, RecommendationResult)
            ]
        )


async def accept_direct(env, req):
    source = AsyncMock()
    source.region.return_value = "seoul"
    decision = await RecommendationConditionsValidator(source).validate(req)
    async with env.writers.begin() as session:
        # This must succeed even while the recommendation is awaiting a provider.
        await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
        await RecommendationConditionsService(RecommendationConditionsRepository(session)).accept(
            req, decision.update
        )
    return decision.update


def test_concurrent_replay_publishes_only_after_commit_with_one_token(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            entered, release = asyncio.Event(), asyncio.Event()

            async def gate(session):
                assert (
                    await session.scalar(select(func.count()).select_from(RecommendationResult))
                    == 1
                )
                entered.set()
                await release.wait()

            env.persistence.writers = WriteHook(env.writers, gate)
            req = request()
            req.conditions_patch.headcount = 3
            first = asyncio.create_task(env.service.execute(req))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(env.service.execute(req.model_copy(deep=True)))
                entry = env.store.trip(req.trip_id)
                assert entry.version is None and entry.conditions is None
                assert not entry.search.pages and not entry.search.seen
                assert not entry.excluded and not entry.requests
                assert await counts(env) == (0, 0, 0)
                assert not first.done() and not second.done()
                release.set()
                one, two = await asyncio.wait_for(asyncio.gather(first, second), 5)
            finally:
                release.set()
                await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
            assert one.failure is None and two.failure is None
            assert one.response == two.response
            assert len(one.response.result["context_token"]) == 43
            assert env.source.search.await_count == 1
            assert env.source.route.await_count == 15
            assert entry.version == 1 and entry.excluded == set(ids(one))
            assert len(entry.search.remaining) == 12 and len(entry.requests) == 1
            assert await counts(env) == (1, 2, 1)
            async with env.readers() as session:
                stored = await RecommendationContextService(
                    RecommendationContextRepository(session)
                ).resolve(one.response.result["context_token"])
                assert stored.conditions == one.response.condition_update.conditions
            # Caller mutation cannot corrupt the stored replay response.
            one.response.result["cards"].clear()
            assert (await env.service.execute(req)).response == two.response

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure_stage", ["result_write", "commit"])
def test_failed_write_or_commit_preserves_pages_candidates_and_exclusions(
    normalized_database, monkeypatch, existing, failure_stage
):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            if existing:
                first = await env.service.execute(req)
                assert first.failure is None
                req = next_request(req, first)
            entry = env.store.trips.get(str(req.trip_id))
            old_search = deepcopy(entry.search) if entry else None
            old_excluded = set(entry.excluded) if entry else set()
            old_counts = await counts(env)
            req.excluded_place_ids = ["kakao:14"]

            async def fail_commit(session):
                # The current-version FK is deferred: statements succeed and the
                # actual COMMIT fails, rolling back conditions and the result.
                await session.execute(update(ChatSession).values(condition_version=999))

            save = RecommendationContextRepository.save

            async def fail_result(repo, snapshot):
                await save(repo, snapshot)
                raise ServiceError("DATA_UNAVAILABLE", "테스트 결과 저장 실패")

            if failure_stage == "commit":
                env.persistence.writers = WriteHook(env.writers, fail_commit)
            else:
                monkeypatch.setattr(RecommendationContextRepository, "save", fail_result)
            failed = await env.service.execute(req)
            assert failed.failure.code == "DATA_UNAVAILABLE"
            assert failed.response.condition_update.applied is False
            assert failed.response.result is None
            entry = env.store.trip(req.trip_id)
            assert entry.version == (0 if existing else None)
            assert entry.excluded == old_excluded
            if existing:
                assert entry.search == old_search
            else:
                assert (
                    not entry.search.pages and not entry.search.remaining and not entry.search.seen
                )
            assert await counts(env) == old_counts
            calls = list(env.source.mock_calls)
            replay = await env.service.execute(req)
            assert replay.response == failed.response and replay.failure.code == failed.failure.code
            assert env.source.mock_calls == calls

            env.persistence.writers = env.writers
            monkeypatch.setattr(RecommendationContextRepository, "save", save)
            retry = req.model_copy(
                update={"request_id": uuid4(), "excluded_place_ids": []}, deep=True
            )
            recovered = await env.service.execute(retry)
            assert recovered.failure is None
            assert ids(recovered) == (
                ["kakao:11", "kakao:12", "kakao:13"]
                if existing
                else ["kakao:0", "kakao:1", "kakao:10"]
            )
            assert "kakao:14" not in entry.excluded
            assert await counts(env) == (1, 1, 2 if existing else 1)
            if not existing:
                assert [call.args[-1] for call in env.source.search.await_args_list] == [1, 1]

    asyncio.run(run())


def test_provider_failure_commits_only_conditions_and_replays_failure_metadata(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            req.conditions_patch.headcount = 3
            req.excluded_place_ids = ["manual"]
            env.source.search.side_effect = ServiceError(
                "RATE_LIMITED", "호출 제한", retry_after_seconds=30
            )
            failed = await env.service.execute(req)
            assert failed.failure.code == "RATE_LIMITED"
            assert failed.response.condition_update.applied
            assert failed.response.condition_update.version == 1
            assert await counts(env) == (1, 2, 0)
            entry = env.store.trip(req.trip_id)
            assert entry.version == 1 and entry.excluded == {"manual"}
            assert not entry.search.pages and not entry.search.seen
            replay = await env.service.execute(req)
            assert replay.response == failed.response
            assert replay.failure.retry_after_seconds == 30
            assert env.source.search.await_count == 1
            env.source.search.side_effect = None
            env.source.search.return_value = ([candidate(i) for i in range(3)], True)
            recovered = await env.service.execute(next_request(req, failed))
            assert recovered.failure is None and await counts(env) == (1, 2, 1)

    asyncio.run(run())


def test_validation_failure_never_creates_or_applies_conditions(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            req.current_conditions.origin = None
            failed = await env.service.execute(req)
            assert failed.failure.code == "VALIDATION_ERROR"
            assert not failed.response.condition_update.applied
            assert await counts(env) == (0, 0, 0)
            env.source.search.assert_not_awaited()
            assert env.store.trip(req.trip_id).version is None
            assert (await env.service.execute(req)).response == failed.response

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["general_candidates", "no_candidates"])
def test_non_recommendation_outcomes_preserve_conditions_without_success_exclusions(
    normalized_database, kind
):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            if kind == "general_candidates":
                env.source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "경로 실패")
            else:
                env.source.search.side_effect = None
                env.source.search.return_value = ([], True)
            result = await env.service.execute(req)
            assert result.failure is None and result.response.kind == kind
            assert result.response.condition_update.applied
            assert not env.store.trip(req.trip_id).excluded
            assert await counts(env) == (1, 1, int(kind == "general_candidates"))
            if kind == "general_candidates":
                assert result.response.result["context_token"]
                assert len(env.store.trip(req.trip_id).search.remaining) == 45
            else:
                assert result.response.result is None
                assert env.store.trip(req.trip_id).search.ended

    asyncio.run(run())


@pytest.mark.parametrize("version", [0, 1])
def test_memory_loss_cannot_restart_a_saved_trip_even_at_version_zero(normalized_database, version):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            if version:
                req.conditions_patch.headcount = 3
            first = await env.service.execute(req)
            assert first.failure is None
            assert first.response.condition_update.version == version
            calls = list(env.source.mock_calls)
            env.service.execution = RecommendationExecution(MemoryProgressStore(), env.persistence)
            failed = await env.service.execute(next_request(req, first))
            assert failed.failure.code == "CONTEXT_EXPIRED"
            assert not failed.response.condition_update.applied
            assert env.source.mock_calls == calls
            assert await counts(env) == (1, 1 + version, 1)

    asyncio.run(run())


def test_database_memory_condition_mismatch_stops_before_external_calls(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            first = await env.service.execute(req)
            assert first.failure is None
            following = next_request(req, first)
            concurrent = following.model_copy(deep=True)
            concurrent.conditions_patch.headcount = 9
            await accept_direct(env, concurrent)
            calls = list(env.source.mock_calls)
            failed = await env.service.execute(following)
            assert failed.failure.code == "CONTEXT_CONDITIONS_MISMATCH"
            assert not failed.response.condition_update.applied
            assert env.source.mock_calls == calls
            assert env.store.trip(req.trip_id).version == 0
            assert await counts(env) == (1, 2, 1)

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
def test_provider_wait_has_no_db_write_lock_and_final_save_rechecks_current_state(
    normalized_database, existing
):
    async def run():
        async with runtime(normalized_database) as env:
            req = request()
            if existing:
                first = await env.service.execute(req)
                assert first.failure is None
                req = next_request(req, first)
            old_count = await counts(env)
            old_progress = deepcopy(env.store.trip(req.trip_id).search) if existing else None
            entered, release = asyncio.Event(), asyncio.Event()

            async def route(origin, destination):
                entered.set()
                await release.wait()
                return 600, 1000

            env.source.route.side_effect = route
            task = asyncio.create_task(env.service.execute(req))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                concurrent = req.model_copy(update={"request_id": uuid4()}, deep=True)
                if existing:
                    concurrent.conditions_patch.headcount = 9
                update_value = await accept_direct(env, concurrent)
                release.set()
                failed = await asyncio.wait_for(task, 5)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            assert failed.failure.code == (
                "CONTEXT_CONDITIONS_MISMATCH" if existing else "CONTEXT_EXPIRED"
            )
            assert not failed.response.condition_update.applied
            assert (await counts(env))[2] == old_count[2]
            if existing:
                assert env.store.trip(req.trip_id).search == old_progress
            else:
                assert not env.store.trip(req.trip_id).search.pages
            async with env.readers() as session:
                current = await RecommendationConditionsRepository(session).get_current(req.trip_id)
                assert current.conditions == update_value.conditions

    asyncio.run(run())


def test_replay_preserves_original_token_expiry_and_does_not_refresh_search_ttl(
    normalized_database,
):
    async def run():
        now = [0.0]
        store = MemoryProgressStore(ttl_seconds=10, clock=lambda: now[0])
        async with runtime(normalized_database, store=store, ttl=1) as env:
            req = request()
            first = await env.service.execute(req)
            token = first.response.result["context_token"]
            expires = first.response.result["context_expires_at"]
            now[0] = 9
            replay = await env.service.execute(req)
            assert replay.response == first.response
            assert env.store.trip(req.trip_id).expires_at == 10
            async with env.readers() as session:
                contexts = RecommendationContextService(
                    RecommendationContextRepository(session),
                    clock=lambda: datetime.fromisoformat(expires) + timedelta(seconds=1),
                )
                with pytest.raises(ServiceError) as error:
                    await contexts.resolve(token)
                assert error.value.code == "CONTEXT_EXPIRED"
            now[0] = 11
            expired = await env.service.execute(req)
            assert expired.failure.code == "CONTEXT_EXPIRED"
            assert env.source.search.await_count == 1
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())


def test_lost_commit_acknowledgement_does_not_recreate_results(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:

            class LostAcknowledgement:
                @asynccontextmanager
                async def begin(self):
                    async with env.writers.begin() as session:
                        yield session
                    # The DB really committed, but simulate a lost connection
                    # before the caller can know whether it succeeded.
                    raise DBAPIError(
                        None, None, RuntimeError("connection lost"), connection_invalidated=True
                    )

            env.persistence.writers = LostAcknowledgement()
            req = request()
            failed = await env.service.execute(req)
            assert failed.failure.code == "DATA_UNAVAILABLE"
            assert failed.failure.retryable is False
            assert not failed.response.condition_update.applied
            assert failed.response.result is None
            assert env.store.trip(req.trip_id).expired
            assert await counts(env) == (1, 1, 1)
            calls = list(env.source.mock_calls)
            assert (await env.service.execute(req)).failure.code == "CONTEXT_EXPIRED"
            assert env.source.mock_calls == calls
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())
