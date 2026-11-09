"""Replay, concurrent selection and publication boundaries with real C01/R01 services."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.schemas.pending import ChatRequest
from app.schemas.recommendations import RecommendationRequest
from app.services.chat import RecommendationChatProcessor
from app.services.origin_context import OriginContextStore
from app.services.origin_search import OriginSearchService
from app.services.pending import PendingService
from app.services.recommendation_persistence import CommitUncertain, RecommendationPersistence
from tests.unit.recommendation.test_progress import request, setup
from tests.unit.services.test_origin_search import poi, provider_for


def harness():
    service, source, _ = setup()
    clock = [datetime.now(UTC)]
    origins = OriginContextStore(clock=lambda: clock[0])
    provider = provider_for(poi(1), poi(2))
    model = AsyncMock()
    model.complete.return_value = Interpretation(intent="recommendation", origin_name="시청역")
    chat = RecommendationChatProcessor(
        model,
        provider,
        service,
        execution=service.execution,
        origin_contexts=origins,
        origin_search=OriginSearchService(provider),
    )
    body = ChatRequest(**request().model_dump(exclude={"conditions_patch"}), message="시청역 출발")
    return SimpleNamespace(
        service=service,
        source=source,
        model=model,
        provider=provider,
        origins=origins,
        clock=clock,
        execution=service.execution,
        send=PendingService(chat=chat).conversation,
        body=body,
    )


def next_request(h, question=None, *, action=None, index=0, **changes):
    values = {"request_id": str(uuid4()), "message": "선택"}
    if action:
        values["origin_action"] = {
            "action": action,
            "context_token": question.origin_context.context_token,
        }
        if action == "select":
            values["origin_action"]["candidate_id"] = question.choices[index].value["candidate_id"]
    elif question:
        values["pending_clarification"] = question.model_dump(mode="json")
    return ChatRequest.model_validate(h.body.model_dump(mode="json") | values | changes)


async def begin(h):
    first = await h.send(h.body)
    assert first.failure is None and not first.response.condition_update.applied
    return first.response.clarification


def calls(h):
    return (list(h.source.mock_calls), list(h.model.mock_calls), list(h.provider.mock_calls))


def state(h):
    trip = h.execution.store.trip(h.body.trip_id)
    return deepcopy((trip.version, trip.conditions, trip.excluded, trip.search))


def persistence(h):
    port = AsyncMock(spec=RecommendationPersistence)
    port.commit.side_effect = lambda req, response, work, **kw: response
    h.execution.persistence = port
    return port


def assert_unapplied(outcome, req, code):
    assert outcome.failure.code == code
    assert not outcome.response.condition_update.applied
    assert outcome.response.condition_update.conditions == req.current_conditions
    assert outcome.response.condition_update.version == req.condition_version
    assert outcome.response.condition_update.changes == []


@pytest.mark.parametrize("change", ["expired", "replaced", "selected", "conditions", "memory_lost"])
def test_cached_initial_question_rechecks_its_output_context(change):
    async def run():
        h = harness()
        question = await begin(h)
        if change == "expired":
            h.clock[0] += timedelta(minutes=5)
        elif change == "replaced":
            await h.send(next_request(h, question, action="restart"))
        elif change == "selected":
            await h.send(next_request(h, question, action="select"))
        elif change == "conditions":
            changed = RecommendationRequest.model_validate(
                request().model_dump()
                | h.body.model_dump(
                    exclude={
                        "message",
                        "recent_messages",
                        "pending_clarification",
                        "origin_action",
                        "target",
                    }
                )
                | {"request_id": str(uuid4()), "conditions_patch": {"headcount": 4}}
            )
            assert (await h.service.execute(changed)).failure is None
        else:
            h.origins.entries.clear()
        before, previous = calls(h), state(h)
        replay = await h.send(h.body)
        code = (
            "CONTEXT_EXPIRED"
            if change in {"expired", "memory_lost"}
            else "CONTEXT_CONDITIONS_MISMATCH"
        )
        assert_unapplied(replay, h.body, code)
        assert replay.response.stages.interpretation == "skipped"
        assert calls(h) == before and state(h) == previous

    asyncio.run(run())


def test_cached_question_rechecks_database_and_expiry_after_database_wait():
    async def run():
        h = harness()
        await begin(h)
        port = persistence(h)

        async def expire(*args):
            h.clock[0] += timedelta(minutes=5)

        port.check_current.side_effect = expire
        before = calls(h)
        outcome = await h.send(h.body)
        assert_unapplied(outcome, h.body, "CONTEXT_EXPIRED")
        assert calls(h) == before
        port.check_current.assert_awaited_once()
        port.commit.assert_not_awaited()

    asyncio.run(run())


def test_live_question_replay_preserves_token_and_never_refreshes_expiry():
    async def run():
        h = harness()
        question = await begin(h)
        h.clock[0] += timedelta(minutes=1)
        before = calls(h)
        replay = await h.send(h.body)
        assert replay.failure is None and replay.response.clarification == question
        assert len(h.origins.entries) == 1 and not h.origins.reserved
        assert calls(h) == before

    asyncio.run(run())


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_selection_commits_once_and_never_consumes_before_commit(same_request):
    async def run():
        h = harness()
        question = await begin(h)
        token = question.origin_context.context_token
        port = persistence(h)
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(req, response, work, **kw):
            entered.set()
            await release.wait()
            return response

        port.commit.side_effect = commit
        first_request = next_request(h, question, action="select")
        second_request = (
            first_request if same_request else next_request(h, question, action="select", index=1)
        )
        first = asyncio.create_task(h.send(first_request))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(h.send(second_request))
        await asyncio.sleep(0)
        assert not second.done()
        assert h.origins.entries[token].used_by is None
        assert state(h)[0] is None
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert a.failure is None and a.response.condition_update.applied
        if same_request:
            assert b.failure is None and b.response == a.response
        else:
            assert_unapplied(b, second_request, "CONTEXT_CONDITIONS_MISMATCH")
        port.commit.assert_awaited_once()
        h.source.search.assert_awaited_once()
        h.provider.keyword.assert_awaited_once()
        h.model.complete.assert_awaited_once()
        assert h.origins.entries[token].used_by == first_request.request_id

    asyncio.run(run())


def test_concurrent_new_search_rejects_queued_old_choice():
    async def run():
        h = harness()
        question = await begin(h)
        entered, release = asyncio.Event(), asyncio.Event()
        rows = h.provider.keyword.return_value

        async def lookup(*args, **kw):
            entered.set()
            await release.wait()
            return rows

        h.provider.keyword.side_effect = lookup
        search_request = next_request(h, question, message="다시 시청역 검색")
        first = asyncio.create_task(h.send(search_request))
        await asyncio.wait_for(entered.wait(), 5)
        selected = next_request(h, question, action="select")
        second = asyncio.create_task(h.send(selected))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert a.failure is None and a.response.kind == "clarification"
        assert_unapplied(b, selected, "CONTEXT_CONDITIONS_MISMATCH")
        h.source.search.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["candidate", "action", "token", "pending_null"])
def test_same_request_id_with_different_selection_or_patch_conflicts(change):
    async def run():
        h = harness()
        question = await begin(h)
        selected = next_request(h, question, action="select")
        assert (await h.send(selected)).failure is None
        altered = selected.model_dump(mode="json")
        if change == "candidate":
            altered["origin_action"]["candidate_id"] = question.choices[1].value["candidate_id"]
        elif change == "action":
            altered["origin_action"]["action"] = "restart"
            altered["origin_action"]["candidate_id"] = None
        elif change == "token":
            altered["origin_action"]["context_token"] = "x" * 43
        else:
            altered["pending_clarification"] = question.model_dump(mode="json")
            altered["pending_clarification"]["pending_patch"]["headcount"] = None
        altered = ChatRequest.model_validate(altered)
        before = calls(h)
        assert_unapplied(await h.send(altered), altered, "REQUEST_ID_CONFLICT")
        assert calls(h) == before

    asyncio.run(run())


def test_finished_selection_replays_after_later_condition_change_but_not_after_expiry():
    async def run():
        h = harness()
        question = await begin(h)
        selected = next_request(h, question, action="select")
        original = await h.send(selected)
        update = original.response.condition_update
        changed = RecommendationRequest(
            request_id=uuid4(),
            trip_id=h.body.trip_id,
            condition_version=update.version,
            current_conditions=update.conditions,
            excluded_place_ids=[],
            conditions_patch={"headcount": 8},
        )
        assert (await h.service.execute(changed)).failure is None
        before, previous = calls(h), state(h)
        replay = await h.send(selected)
        assert replay.failure is None and replay.response == original.response
        assert calls(h) == before and state(h) == previous
        h.clock[0] += timedelta(minutes=5)
        assert_unapplied(await h.send(selected), selected, "CONTEXT_EXPIRED")
        assert calls(h) == before and state(h) == previous

    asyncio.run(run())


def test_expiry_during_preflight_stops_before_interpretation_or_recommendation():
    async def run():
        h = harness()
        question = await begin(h)
        port = persistence(h)

        async def expire(*args):
            h.clock[0] += timedelta(minutes=5)

        port.check_current.side_effect = expire
        h.model.complete.return_value = Interpretation(intent="recommendation", headcount=2)
        before = calls(h)
        body = next_request(h, question, message="2명")
        assert_unapplied(await h.send(body), body, "CONTEXT_EXPIRED")
        assert calls(h) == before
        port.commit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["interpretation", "recommendation"])
def test_expiry_after_provider_work_never_publishes_conditions_or_consumes_choice(phase):
    async def run():
        h = harness()
        question = await begin(h)
        token = question.origin_context.context_token
        port = persistence(h)
        previous = state(h)
        if phase == "interpretation":

            async def expire(*args):
                h.clock[0] += timedelta(minutes=5)
                return Interpretation(intent="recommendation", headcount=2)

            h.model.complete.side_effect = expire
            body = next_request(h, question, message="2명")
        else:
            original = h.source.search.side_effect

            async def expire(*args):
                h.clock[0] += timedelta(minutes=5)
                return original(*args)

            h.source.search.side_effect = expire
            body = next_request(h, question, action="select")
        assert_unapplied(await h.send(body), body, "CONTEXT_EXPIRED")
        port.commit.assert_not_awaited()
        assert state(h) == previous
        assert h.origins.entries[token].used_by is None and not h.origins.reserved

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", [SQLAlchemyError("rollback"), ServiceError("CONTEXT_CONDITIONS_MISMATCH", "changed")]
)
def test_failed_commit_keeps_choice_and_search_history_unpublished(failure):
    async def run():
        h = harness()
        question = await begin(h)
        previous = state(h)
        port = persistence(h)
        port.commit.side_effect = failure
        body = next_request(h, question, action="select")
        failed = await h.send(body)
        assert_unapplied(failed, body, getattr(failure, "code", None) or "DATA_UNAVAILABLE")
        assert state(h) == previous
        assert h.origins.entries[question.origin_context.context_token].used_by is None
        before = calls(h)
        replay = await h.send(body)
        assert replay.response == failed.response and calls(h) == before
        port.commit.assert_awaited_once()

    asyncio.run(run())


def test_accepted_condition_failure_is_committed_consumed_and_replayed_once():
    async def run():
        h = harness()
        question = await begin(h)
        port = persistence(h)
        h.source.search.side_effect = ServiceError("RATE_LIMITED", "limit")
        body = next_request(h, question, action="select")
        failed = await h.send(body)
        assert failed.failure.code == "RATE_LIMITED" and failed.response.condition_update.applied
        assert h.origins.entries[question.origin_context.context_token].used_by == body.request_id
        port.commit.assert_awaited_once()
        before = calls(h)
        assert (await h.send(body)).response == failed.response
        again = next_request(h, question, action="select")
        assert_unapplied(await h.send(again), again, "CONTEXT_CONDITIONS_MISMATCH")
        assert calls(h) == before

    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [False, True])
def test_cancellation_or_uncertain_commit_never_reexecutes_selection(uncertain):
    async def run():
        h = harness()
        question = await begin(h)
        port = persistence(h)
        entered = asyncio.Event()

        async def commit(*args, **kw):
            entered.set()
            if uncertain:
                raise CommitUncertain()
            await asyncio.Event().wait()

        port.commit.side_effect = commit
        body = next_request(h, question, action="select")
        if uncertain:
            assert_unapplied(await h.send(body), body, "DATA_UNAVAILABLE")
        else:
            task = asyncio.create_task(h.send(body))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert h.execution.store.trip(body.trip_id).expired
        assert h.origins.entries[question.origin_context.context_token].used_by is None
        assert not h.origins.reserved
        before = calls(h)
        assert_unapplied(await h.send(body), body, "CONTEXT_EXPIRED")
        assert calls(h) == before
        port.commit.assert_awaited_once()

    asyncio.run(run())


def test_full_origin_store_keeps_current_question_selectable():
    async def run():
        h = harness()
        h.origins.capacity = 1
        question = await begin(h)
        body = next_request(h, question, action="restart")
        assert_unapplied(await h.send(body), body, "SERVICE_UNAVAILABLE")
        assert not h.origins.reserved and len(h.origins.entries) == 1
        assert (await h.send(next_request(h, question, action="select"))).failure is None

    asyncio.run(run())


def test_concurrent_initial_search_runs_model_and_provider_once():
    async def run():
        h = harness()
        entered, release = asyncio.Event(), asyncio.Event()
        rows = h.provider.keyword.return_value

        async def lookup(*args, **kw):
            entered.set()
            await release.wait()
            return rows

        h.provider.keyword.side_effect = lookup
        first = asyncio.create_task(h.send(h.body))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(h.send(h.body))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert a.failure is None and b.failure is None and a.response == b.response
        h.model.complete.assert_awaited_once()
        h.provider.keyword.assert_awaited_once()
        assert len(h.origins.entries) == 1 and not h.origins.reserved

    asyncio.run(run())


def test_queued_choice_rechecks_expiry_after_lock_wait():
    async def run():
        h = harness()
        question = await begin(h)
        entered, release = asyncio.Event(), asyncio.Event()

        async def interpret(*args):
            entered.set()
            await release.wait()
            return Interpretation(intent="recommendation", headcount=2)

        h.model.complete.side_effect = interpret
        changing = next_request(h, question, message="2명")
        first = asyncio.create_task(h.send(changing))
        await asyncio.wait_for(entered.wait(), 5)
        selected = next_request(h, question, action="select")
        second = asyncio.create_task(h.send(selected))
        await asyncio.sleep(0)
        assert not second.done()
        h.clock[0] += timedelta(minutes=5)
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert_unapplied(a, changing, "CONTEXT_EXPIRED")
        assert_unapplied(b, selected, "CONTEXT_EXPIRED")
        h.source.search.assert_not_awaited()
        assert not h.origins.reserved

    asyncio.run(run())


def test_origin_expiry_preserves_existing_trip_and_allows_fresh_search():
    async def run():
        h = harness()
        initial = RecommendationRequest.model_validate(
            h.body.model_dump(
                exclude={
                    "message",
                    "recent_messages",
                    "pending_clarification",
                    "origin_action",
                    "target",
                }
            )
            | {"conditions_patch": {}, "request_id": str(uuid4())}
        )
        accepted = await h.service.execute(initial)
        assert accepted.failure is None
        h.body.current_conditions = accepted.response.condition_update.conditions
        h.body.condition_version = accepted.response.condition_update.version
        question = await begin(h)
        previous = state(h)
        assert previous[2]  # A real recommendation history exists before origin expiry.
        h.clock[0] += timedelta(minutes=5)
        before = calls(h)
        selected = next_request(h, question, action="select")
        assert_unapplied(await h.send(selected), selected, "CONTEXT_EXPIRED")
        assert state(h) == previous and calls(h) == before
        assert not h.execution.store.trip(h.body.trip_id).expired
        fresh = await h.send(next_request(h, message="시청역 다시 검색"))
        assert fresh.failure is None and fresh.response.kind == "clarification"
        assert state(h) == previous
        assert (
            fresh.response.clarification.origin_context.context_token
            != question.origin_context.context_token
        )
        assert question.origin_context.context_token not in h.origins.entries
        assert len(h.origins.entries) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        SQLAlchemyError("private DB information"),
        ServiceError("CONTEXT_CONDITIONS_MISMATCH", "changed"),
    ],
)
def test_cached_question_database_failure_leaves_cache_and_trip_intact(failure):
    async def run():
        h = harness()
        question = await begin(h)
        port = persistence(h)
        port.check_current.side_effect = failure
        before, previous = calls(h), state(h)
        failed = await h.send(h.body)
        assert_unapplied(failed, h.body, getattr(failure, "code", None) or "DATA_UNAVAILABLE")
        assert "private DB information" not in failed.response.message
        assert calls(h) == before and state(h) == previous
        assert not h.execution.store.trip(h.body.trip_id).expired
        port.check_current.side_effect = None
        recovered = await h.send(h.body)
        assert recovered.failure is None and recovered.response.clarification == question
        assert calls(h) == before

    asyncio.run(run())


def test_replayed_transition_validates_new_question_while_allowing_consumed_input():
    async def run():
        h = harness()
        question = await begin(h)
        body = next_request(h, question, action="restart")
        original = await h.send(body)
        new_question = original.response.clarification
        before = calls(h)
        assert (await h.send(body)).response == original.response
        assert calls(h) == before
        changed = next_request(h, new_question, message="다시 검색")
        assert (await h.send(changed)).failure is None
        before = calls(h)
        assert_unapplied(await h.send(body), body, "CONTEXT_CONDITIONS_MISMATCH")
        assert calls(h) == before

    asyncio.run(run())
