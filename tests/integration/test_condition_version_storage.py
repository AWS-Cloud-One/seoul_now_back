"""Accepted decisions and historical results share one real, isolated DB transaction."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import ServiceError
from app.db.session import read_session_factory, write_session_factory
from app.models import (
    ChatConditionAgeGroup,
    ChatConditionPurpose,
    ChatConditionVersion,
    ChatSession,
    RecommendationResult,
)
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.recommendations import Conditions, RecommendationRequest
from app.services.recommendation_adapters import RecommendationConditionsValidator
from app.services.recommendation_conditions import RecommendationConditionsService
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.current_recommendation import current_result
from tests.fixtures.recommendation_context import response

pytestmark = pytest.mark.integration


@asynccontextmanager
async def database(url):
    engine = create_async_engine(url)
    try:
        yield write_session_factory(engine), read_session_factory(engine)
    finally:
        await engine.dispose()


def base_conditions():
    values = response().condition_update.conditions.model_dump()
    values.update(
        age_groups=["thirties", "twenties"],
        purposes=[{"purpose": "cafe", "keyword": "조용한"}, {"purpose": "walk"}],
    )
    return Conditions.model_validate(values)


def request_for(conditions=None, *, trip_id=None, version=0, patch=None):
    return RecommendationRequest(
        request_id=uuid4(),
        trip_id=trip_id or uuid4(),
        condition_version=version,
        current_conditions=(conditions or base_conditions()).model_copy(deep=True),
        conditions_patch=patch or {},
        excluded_place_ids=[],
    )


async def decision(request):
    source = AsyncMock()
    source.region.return_value = "seoul"
    return (await RecommendationConditionsValidator(source).validate(request)).update


def writer(session):
    return RecommendationConditionsService(RecommendationConditionsRepository(session))


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("empty_baseline", [False, True])
def test_initial_baseline_and_accepted_version_then_noop(normalized_database, empty_baseline):
    async def run():
        initial = base_conditions()
        if empty_baseline:
            initial = Conditions.model_validate(
                {key: [] if key == "purposes" else None for key in Conditions.model_fields}
            )
        request = request_for(
            initial, patch=base_conditions().model_dump() if empty_baseline else {}
        )
        update = await decision(request)
        async with database(normalized_database) as (writers, readers):
            async with writers.begin() as session:
                assert await writer(session).accept(request, update) == update
                assert not session.new and not session.dirty
                # Saving conditions is not an implicit commit.
                async with readers() as other:
                    assert (
                        await RecommendationConditionsRepository(other).get_current(request.trip_id)
                        is None
                    )
            noop = request_for(update.conditions, trip_id=request.trip_id, version=update.version)
            noop_update = await decision(noop)
            async with writers.begin() as session:
                timestamp = await session.scalar(select(ChatSession.updated_at))
                assert await writer(session).accept(noop, noop_update) == noop_update
                assert await session.scalar(select(ChatSession.updated_at)) == timestamp
            async with readers() as session:
                repo = RecommendationConditionsRepository(session)
                state = await repo.get_current(request.trip_id)
                assert state.version == int(empty_baseline)
                assert state.conditions == base_conditions()
                assert await repo.get_version(request.trip_id, 0) == initial
                assert await count(session, ChatConditionVersion) == 1 + int(empty_baseline)
                assert await count(session, ChatConditionPurpose) == 2
                assert await count(session, ChatConditionAgeGroup) == 2
                assert not session.identity_map

    asyncio.run(run())


def test_changed_values_clear_and_revert_preserve_each_historical_version(normalized_database):
    async def run():
        initial = base_conditions()
        origin = initial.origin.model_dump()
        origin.update(
            label="새 출발지",
            address="새 주소",
            region=None,
            supported=False,
            coordinates={"latitude": 37.56, "longitude": 126.98},
        )
        patches = [
            {},
            {
                "origin": origin,
                "purposes": [{"purpose": "walk"}, {"purpose": "cafe", "keyword": None}],
                "age_groups": ["twenties", "thirties"],
                "group_type": "family",
                "headcount": 4,
                "max_travel_seconds": 900,
            },
            {"group_type": None, "age_groups": None, "headcount": None, "max_travel_seconds": None},
            initial.model_dump(),
        ]
        trip_id, current, version, history = uuid4(), initial, 0, []
        async with database(normalized_database) as (writers, readers):
            for patch in patches:
                request = request_for(current, trip_id=trip_id, version=version, patch=patch)
                update = await decision(request)
                async with writers.begin() as session:
                    await writer(session).accept(request, update)
                history.append(update.conditions)
                current, version = update.conditions, update.version
            assert version == 3 and current == initial
            assert history[2].age_groups is None and history[2].headcount is None
            async with readers() as session:
                repo = RecommendationConditionsRepository(session)
                assert (await repo.get_current(trip_id)).version == 3
                assert [await repo.get_version(trip_id, v) for v in range(4)] == history
                assert await count(session, ChatConditionVersion) == 4
                assert await count(session, ChatConditionPurpose) == 8
                assert await count(session, ChatConditionAgeGroup) == 6

    asyncio.run(run())


@pytest.mark.parametrize(
    "conflict", ["old_version", "future_version", "different_values", "missing_trip"]
)
def test_stale_or_forged_bases_do_not_change_stored_state(normalized_database, conflict):
    async def run():
        initial = request_for(patch={"headcount": 3})
        update = await decision(initial)
        bad = request_for(update.conditions, trip_id=initial.trip_id, version=1)
        if conflict == "old_version":
            bad = initial.model_copy(deep=True)
        elif conflict == "future_version":
            bad.condition_version = 9
        elif conflict == "different_values":
            bad.current_conditions.headcount = 99
        else:
            bad.trip_id = uuid4()
        bad_update = await decision(bad)
        async with database(normalized_database) as (writers, readers):
            async with writers.begin() as session:
                await writer(session).accept(initial, update)
            async with writers.begin() as session:
                with pytest.raises(ServiceError) as error:
                    await writer(session).accept(bad, bad_update)
                assert error.value.code == (
                    "CONTEXT_EXPIRED"
                    if conflict == "missing_trip"
                    else "CONTEXT_CONDITIONS_MISMATCH"
                )
                assert error.value.retryable is False
            async with readers() as session:
                current = await RecommendationConditionsRepository(session).get_current(
                    initial.trip_id
                )
                assert current.version == 1 and current.conditions == update.conditions
                assert await count(session, ChatSession) == 1
                assert await count(session, ChatConditionVersion) == 2

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
def test_child_insert_failure_rolls_back_acceptance_and_leaves_transaction_usable(
    normalized_database, existing
):
    async def run():
        initial = request_for()
        initial_update = await decision(initial)
        changed = request_for(trip_id=initial.trip_id, patch={"age_groups": ["forties"]})
        update = await decision(changed)
        broken = update.model_copy(deep=True)
        # Deliberately bypass the trusted schema to force a DB failure after the
        # session, version and purposes have been inserted, at the last child.
        broken.conditions.age_groups = ["not_an_age_group"]
        broken.changes[0].after = ["not_an_age_group"]
        async with database(normalized_database) as (writers, readers):
            if existing:
                async with writers.begin() as session:
                    await writer(session).accept(initial, initial_update)
            async with writers.begin() as session:
                with pytest.raises(ServiceError) as error:
                    await writer(session).accept(changed, broken)
                assert error.value.code == "DATA_UNAVAILABLE"
                assert await count(session, ChatSession) == int(existing)
                assert await count(session, ChatConditionVersion) == int(existing)
                assert await count(session, ChatConditionPurpose) == 2 * int(existing)
                assert await count(session, ChatConditionAgeGroup) == 2 * int(existing)
                await writer(session).accept(changed, update)
            async with readers() as session:
                repo = RecommendationConditionsRepository(session)
                assert (await repo.get_current(initial.trip_id)).conditions == update.conditions
                assert await repo.get_version(initial.trip_id, 0) == initial.current_conditions

    asyncio.run(run())


async def wait_until_blocked(session, pid):
    # Observe a real PostgreSQL lock wait, rather than relying on task timing.
    async def poll():
        while not await session.scalar(  # noqa: ASYNC110 -- PostgreSQL exposes no asyncio event.
            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.parametrize(
    "mode",
    ["existing", "new_changed", "new_same", "new_other_base", "new_noop_then_change", "rollback"],
)
def test_concurrent_acceptance_serializes_current_pointer_and_initial_creation(
    normalized_database, mode
):
    async def run():
        baseline = request_for()
        first = request_for(
            trip_id=baseline.trip_id,
            patch={}
            if mode in {"new_same", "new_other_base", "new_noop_then_change"}
            else {"headcount": 3},
        )
        second = request_for(
            trip_id=baseline.trip_id,
            patch={} if mode in {"new_same", "new_other_base"} else {"headcount": 4},
        )
        if mode == "new_other_base":
            second.current_conditions.headcount = 99
        first_update, second_update = await decision(first), await decision(second)
        baseline_update = await decision(baseline)
        async with database(normalized_database) as (writers, readers):
            if mode == "existing":
                async with writers.begin() as session:
                    await writer(session).accept(baseline, baseline_update)
            async with writers.begin() as waiter, writers() as owner:
                # Start the waiting transaction first, also exercising timestamp
                # ordering when the other transaction creates the trip later.
                pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
                transaction = await owner.begin()
                task = None
                try:
                    await writer(owner).accept(first, first_update)
                    task = asyncio.create_task(writer(waiter).accept(second, second_update))
                    async with readers() as observer:
                        await wait_until_blocked(observer, pid)
                    if mode == "rollback":
                        await transaction.rollback()
                    else:
                        await transaction.commit()
                    if mode in {"existing", "new_changed", "new_other_base"}:
                        with pytest.raises(ServiceError) as error:
                            await asyncio.wait_for(task, 5)
                        assert error.value.code == "CONTEXT_CONDITIONS_MISMATCH"
                        expected = first_update
                    else:
                        assert await asyncio.wait_for(task, 5) == second_update
                        expected = second_update
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            async with readers() as session:
                repo = RecommendationConditionsRepository(session)
                state = await repo.get_current(baseline.trip_id)
                assert state.version == expected.version and state.conditions == expected.conditions
                assert await count(session, ChatSession) == 1
                assert await count(session, ChatConditionVersion) == expected.version + 1
                assert await repo.get_version(baseline.trip_id, 0) == baseline.current_conditions

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["empty_ages", "duplicate_ages", "duplicate_purposes"])
def test_unrepresentable_baseline_is_rejected_instead_of_silently_normalized(
    normalized_database, invalid
):
    async def run():
        request = request_for(patch={"age_groups": ["twenties"], "purposes": [{"purpose": "walk"}]})
        if invalid == "empty_ages":
            request.current_conditions.age_groups = []
        elif invalid == "duplicate_ages":
            request.current_conditions.age_groups = ["twenties", "twenties"]
        else:
            request.current_conditions.purposes = [request.current_conditions.purposes[0]] * 2
        update = await decision(request)
        async with database(normalized_database) as (writers, _):
            async with writers.begin() as session:
                with pytest.raises(ServiceError) as error:
                    await writer(session).accept(request, update)
                assert error.value.code == "VALIDATION_ERROR"
                assert await count(session, ChatSession) == 0
                assert await count(session, ChatConditionVersion) == 0

    asyncio.run(run())


@pytest.mark.parametrize("fail_result", [False, True])
def test_conditions_and_actual_engine_result_commit_or_rollback_together(
    normalized_database, fail_result
):
    async def run():
        scenario = await current_result()
        # Use a partial initial baseline so this stores both version 0 and 1.
        scenario.request.current_conditions.headcount = None
        scenario.request.conditions_patch.headcount = (
            scenario.data.condition_update.conditions.headcount
        )
        scenario.data.condition_update = await decision(scenario.request)
        async with database(normalized_database) as (writers, readers):

            async def store():
                async with writers.begin() as session:
                    await writer(session).accept(scenario.request, scenario.data.condition_update)
                    contexts = RecommendationContextService(
                        RecommendationContextRepository(session)
                    )
                    registered = await contexts.register(scenario.request.request_id, scenario.data)
                    if fail_result:
                        # Fail after both aggregates are written, before commit.
                        await session.execute(
                            ChatSession.__table__.insert().values(trip_id=scenario.request.trip_id)
                        )
                    return registered

            if fail_result:
                with pytest.raises(IntegrityError):
                    await store()
            else:
                registered = await store()
            async with readers() as session:
                assert await count(session, ChatSession) == int(not fail_result)
                assert await count(session, ChatConditionVersion) == 2 * int(not fail_result)
                assert await count(session, RecommendationResult) == int(not fail_result)
                if not fail_result:
                    stored = await RecommendationContextService(
                        RecommendationContextRepository(session)
                    ).resolve(registered.result["context_token"])
                    assert stored.result.condition_version == 1
                    assert stored.conditions == scenario.data.condition_update.conditions

    asyncio.run(run())


def test_repository_requires_caller_transaction(normalized_database):
    async def run():
        async with database(normalized_database) as (writers, _):
            async with writers() as session:
                with pytest.raises(InvalidRequestError, match="caller-owned"):
                    await RecommendationConditionsRepository(session).accept(
                        uuid4(), 0, base_conditions(), base_conditions()
                    )

    asyncio.run(run())
