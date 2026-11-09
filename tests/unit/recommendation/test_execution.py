import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.schemas.pending import ChatRequest
from app.services.recommendation_persistence import RecommendationPersistence
from tests.unit.recommendation.test_progress import ids, next_request, request, setup


@pytest.mark.parametrize("change", ["message", "pending_null", "operation"])
def test_original_conversation_request_and_operation_own_the_replay_identity(change):
    async def run():
        service, source, _ = setup()
        structured = request()
        chat = ChatRequest(
            **structured.model_dump(exclude={"conditions_patch"}),
            message="추천해 줘",
            pending_clarification={
                "question": "어떤 곳?",
                "required_fields": ["purposes"],
                "pending_patch": {},
                "choices": [],
            },
        )
        interpretation_calls = 0

        async def callback(work):
            nonlocal interpretation_calls
            interpretation_calls += 1
            return await service.prepare(structured, work)

        first = await service.execution.run(chat, "chat", callback, service.rejected)
        replay = await service.execution.run(chat, "chat", callback, service.rejected)
        assert replay.response == first.response and interpretation_calls == 1
        changed, operation = chat.model_copy(deep=True), "chat"
        if change == "message":
            changed.message = "다른 추천해 줘"
        elif change == "pending_null":
            changed.pending_clarification.pending_patch.headcount = None
        else:
            operation = "recommend"
        conflict = await service.execution.run(changed, operation, callback, service.rejected)
        assert conflict.failure.code == "REQUEST_ID_CONFLICT"
        assert interpretation_calls == 1 and source.search.await_count == 1

    asyncio.run(run())


def test_failure_while_rechecking_remaining_candidates_discards_partial_work():
    async def run():
        service, source, _ = setup()
        req = request()
        first = await service.execute(req)
        entry = service.execution.store.trip(req.trip_id)
        before = deepcopy(entry.search)
        req = next_request(req, first)
        source.route.side_effect = ServiceError("RATE_LIMITED", "경로 호출 제한")
        failed = await service.execute(req)
        assert failed.failure.code == "RATE_LIMITED" and failed.response.condition_update.applied
        assert entry.search == before and entry.excluded == set(ids(first))
        source.route.side_effect = None
        recovered = await service.execute(next_request(req, failed))
        assert ids(recovered) == ["11", "12", "13"]
        assert source.search.await_count == 1

    asyncio.run(run())


def test_database_preflight_failure_is_unapplied_cached_and_prevents_external_calls():
    async def run():
        service, source, _ = setup()
        persistence = AsyncMock(spec=RecommendationPersistence)
        persistence.check_current.side_effect = SQLAlchemyError("private database detail")
        service.execution.persistence = persistence
        req = request()
        first = await service.execute(req)
        assert first.failure.code == "DATA_UNAVAILABLE"
        assert not first.response.condition_update.applied
        assert "private database detail" not in first.response.message
        assert not source.mock_calls
        assert (await service.execute(req)).response == first.response
        persistence.check_current.assert_awaited_once()
        persistence.commit.assert_not_awaited()

    asyncio.run(run())


def test_cancellation_during_persistence_discards_work_and_refuses_uncertain_resume():
    async def run():
        service, source, _ = setup()
        entered = asyncio.Event()

        async def commit(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        persistence = AsyncMock(spec=RecommendationPersistence)
        persistence.commit.side_effect = commit
        service.execution.persistence = persistence
        req = request()
        task = asyncio.create_task(service.execute(req))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        entry = service.execution.store.trip(req.trip_id)
        assert entry.expired and not entry.excluded and not entry.requests
        assert not entry.search.pages and not entry.search.remaining
        calls = list(source.mock_calls)
        assert (await service.execute(req)).failure.code == "CONTEXT_EXPIRED"
        assert source.mock_calls == calls

    asyncio.run(run())
