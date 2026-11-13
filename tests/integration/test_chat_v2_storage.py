"""Real PostGIS ownership/commit/replay. LLM and provider replies are synthetic."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import read_session_factory, write_session_factory
from app.models import (
    ChatBrowserSession,
    ChatConditionVersion,
    ChatConversation,
    ChatRun,
    ChatRunCommit,
    RecommendationCard,
    RecommendationResult,
)
from app.modules.chat_v2.contracts import InterpretationProposal, RunRequest
from app.modules.chat_v2.execution import build_stored_headcount_execution
from app.modules.chat_v2.finalization import DatabaseFinalizer
from app.modules.chat_v2.results import StageError
from app.modules.chat_v2.storage import ChatV2Store
from app.modules.chat_v2.storage_contracts import ExecutionClaim, ExecutionView, StorageRejected
from app.modules.chat_v2.validation import HeadcountPlanValidator
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.services.driving_requests import DrivingRequests
from tests.fixtures.chat_v2 import scenario
from tests.integration.test_recommendation_geography import seed
from tests.unit.recommendation.test_geography import sections
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


def request_for(conversation, text="3명으로 바꿔줘", **input_values):
    return RunRequest(
        request_id=uuid4(),
        expected_condition_version=conversation.condition_version,
        expected_state_version=conversation.state_version,
        input={"text": text, **input_values},
    )


async def bootstrap(store):
    credential = await store.create_session()
    conditions = scenario()[2].plan.context.current_conditions
    conversation = await store.create_conversation(credential, initial_conditions=conditions)
    return credential, conversation


async def prepare_fixture(store, credential, conversation):
    request = request_for(conversation)
    claim = await store.admit(credential, conversation.conversation_id, request)
    _, proposal, prepared, _ = scenario()
    proposal.decision.evidence_text = request.input.text
    plan = await HeadcountPlanValidator().validate(request, claim.context, proposal)
    prepared.plan = plan
    prepared.prepared_at = store.clock()
    return request, claim, prepared


async def trip_counts(store, trip_id):
    async with store.writers() as db:
        return [
            await db.scalar(select(func.count()).select_from(model).where(model.trip_id == trip_id))
            for model in (ChatConditionVersion, RecommendationResult, ChatRunCommit)
        ]


def test_owned_full_flow_question_recommendation_and_replay(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        readers, writers = read_session_factory(engine), write_session_factory(engine)
        store = ChatV2Store(writers)
        try:
            async with writers.begin() as db:
                observed = await seed(db)
            credential, conversation = await bootstrap(store)
            assert conversation.expires_at <= credential.expires_at
            assert (
                timedelta(hours=23, minutes=59)
                < credential.expires_at - store.clock()
                <= timedelta(hours=24)
            )
            async with writers() as db:
                token_row = await db.get(ChatBrowserSession, credential.session_id)
                assert credential.token.get_secret_value() not in token_row.token_digest

            def respond(req):
                if req.url.path.endswith("coord2regioncode.json"):
                    return httpx.Response(
                        200,
                        json={
                            "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
                        },
                    )
                if req.url.path.endswith("keyword.json"):
                    return httpx.Response(
                        200,
                        json={
                            "documents": [
                                {
                                    "id": "v2-cafe",
                                    "place_name": "검증용 카페",
                                    "category_group_code": "CE7",
                                    "x": "126.98",
                                    "y": "37.57",
                                }
                            ],
                            "meta": {"is_end": True},
                        },
                    )
                if req.url.path == "/v1/directions":
                    return httpx.Response(200, json=route_body())
                if req.url.host == "openapi.seoul.go.kr":
                    return httpx.Response(
                        200,
                        json={
                            "RESULT": {"CODE": "INFO-000"},
                            "CITYDATA": {
                                "AREA_CD": "POI001",
                                "AREA_NM": "명소",
                                **sections(
                                    ("1000001401", "서행"),
                                    ("1000000301", "서행"),
                                    at=observed.isoformat(),
                                ),
                            },
                        },
                    )
                raise AssertionError("Unexpected external request")

            question = InterpretationProposal(
                decision={
                    "kind": "needs_input",
                    "question": "몇 명인가요?",
                    "required_field": "headcount",
                }
            )
            interpreter = AsyncMock()
            interpreter.interpret.return_value = question
            settings = Settings(
                _env_file=None,
                database_url=normalized_database,
                kakao_rest_api_key="synthetic",
                seoul_citydata_api_key="synthetic",
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                execution = build_stored_headcount_execution(
                    store,
                    interpreter,
                    readers,
                    http,
                    settings,
                    driving_requests=DrivingRequests(budget=5, interval=0),
                )
                first = await execution.execute(
                    credential,
                    conversation.conversation_id,
                    request_for(conversation, "인원 좀 늘려줘"),
                )
                assert first.response.status == "waiting_input" and first.state_version == 1
                pending = await store.conversation(credential, conversation.conversation_id)
                assert pending.pending_question is not None and pending.conditions.headcount == 2
                request = request_for(
                    pending,
                    "셋이요",
                    reply_to_clarification_id=pending.pending_question.clarification_id,
                )
                interpreter.interpret.return_value = scenario()[1]
                done = await execution.execute(credential, conversation.conversation_id, request)
                assert done.response.status == "succeeded", done.response.model_dump()
                assert done.response.condition_version == 1 and done.state_version == 2
                card = done.response.recommendation.cards[0]
                assert (
                    card.place.value.place_id == "kakao:v2-cafe"
                    and card.route.value.duration_seconds == 600
                )
                assert card.parking.total_count == 1
                assert (
                    done.response.snapshot.finalization.prepared.search_scope.areas[
                        0
                    ].geometry_version
                    == "boundary-v1"
                )
                # New store/runner instances replay the DB response without interpretation.
                execution.store = ChatV2Store(writers)
                assert (
                    await execution.execute(credential, conversation.conversation_id, request)
                    == done
                )
                assert interpreter.interpret.await_count == 2
                current = await store.conversation(credential, conversation.conversation_id)
                interpreter.interpret.return_value = InterpretationProposal(
                    decision={
                        "kind": "change_headcount",
                        "headcount": 3,
                        "scope": "persistent",
                        "evidence_text": "3명으로 바꿔줘",
                    }
                )
                unchanged = await execution.execute(
                    credential, conversation.conversation_id, request_for(current)
                )
                assert unchanged.response.kind == "unchanged"
                assert unchanged.response.condition_version == 1
                changed = request.model_copy(deep=True)
                changed.input.text = "4명으로 바꿔줘"
                with pytest.raises(StorageRejected, match="REQUEST_ID_CONFLICT"):
                    await execution.execute(credential, conversation.conversation_id, changed)
            assert await trip_counts(store, conversation.trip_id) == [2, 1, 1]
            current = await store.conversation(credential, conversation.conversation_id)
            expected = conversation.conditions.model_copy(update={"headcount": 3})
            assert (
                current.conditions == expected
                and current.pending_question is None
                and current.active_run_id is None
            )
            async with writers() as db:
                assert await db.scalar(select(func.count()).select_from(RecommendationCard)) == 1
                saved = await db.get(ChatConversation, conversation.conversation_id)
                assert saved.context["excluded"] == ["kakao:v2-cafe"]
            other = await store.create_session()
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await store.get_run(other, conversation.conversation_id, done.identity.run_id)
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await store.admit(other, conversation.conversation_id, request)
            invalid = credential.model_copy(update={"token": other.token})
            with pytest.raises(StorageRejected, match="UNAUTHORIZED"):
                await store.conversation(invalid, conversation.conversation_id)
            with pytest.raises(StorageRejected, match="VERSION_CONFLICT"):
                await store.admit(
                    credential, conversation.conversation_id, request_for(conversation)
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["empty", "lookup_failure", "rollback", "lost_ack", "conflict", "expired"]
)
def test_atomic_outcomes_and_commit_reconciliation(normalized_database, monkeypatch, mode):
    async def run():
        engine = create_async_engine(normalized_database)
        store = ChatV2Store(write_session_factory(engine))
        try:
            credential, conversation = await bootstrap(store)
            request, claim, prepared = await prepare_fixture(store, credential, conversation)
            # In-flight same request is replayed; a different request is rejected.
            assert (
                await store.admit(credential, conversation.conversation_id, request)
            ).status == "running"
            with pytest.raises(StorageRejected, match="RUN_ACTIVE"):
                await store.admit(
                    credential, conversation.conversation_id, request_for(conversation)
                )
            finalizer = DatabaseFinalizer(store, claim)
            if mode in {"empty", "lookup_failure"}:
                prepared.cards = []
                prepared.outcome = "no_candidates" if mode == "empty" else "error"
                if mode == "lookup_failure":
                    prepared.error = StageError(
                        code="RECOMMENDATION_FAILED", safe_message="조회 실패", retryable=True
                    )
            elif mode == "rollback":

                async def fail_after_condition(*args, **kwargs):
                    raise RuntimeError("synthetic storage failure")

                monkeypatch.setattr(
                    "app.modules.chat_v2.finalization.save_result", fail_after_condition
                )
            elif mode == "lost_ack":
                actual_commit = finalizer._commit

                async def lost_ack(value):
                    await actual_commit(value)
                    raise ConnectionError("synthetic lost commit acknowledgement")

                monkeypatch.setattr(finalizer, "_commit", lost_ack)
            elif mode == "conflict":
                async with store.writers.begin() as db:
                    await RecommendationConditionsRepository(db).accept(
                        conversation.trip_id,
                        0,
                        conversation.conditions,
                        conversation.conditions.model_copy(update={"headcount": 4}),
                        expected_existing=True,
                    )
            else:
                now = store.clock()
                store.clock = lambda: now + timedelta(hours=25)
            result = await finalizer.finalize(prepared)
            successful = mode in {"empty", "lookup_failure", "lost_ack"}
            assert result.commit.state == ("committed" if successful else "not_applied"), (
                result.model_dump()
            )
            expected = (
                [2, int(mode == "lost_ack"), 1]
                if successful
                else [2 if mode == "conflict" else 1, 0, 0]
            )
            assert await trip_counts(store, conversation.trip_id) == expected
            if successful:
                again = await finalizer.finalize(prepared)
                assert (
                    again == result and await trip_counts(store, conversation.trip_id) == expected
                )
            view = await store.read_claim(claim)
            assert view.status == "finished"
            assert view.response.status == (
                "partial" if mode == "lookup_failure" else "succeeded" if successful else "failed"
            )
            if mode == "expired":
                with pytest.raises(StorageRejected, match="EXPIRED"):
                    await store.conversation(credential, conversation.conversation_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_owner_fencing_and_unresolved_commit_lock(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        store = ChatV2Store(write_session_factory(engine))
        try:
            credential, conversation = await bootstrap(store)
            request, claim, prepared = await prepare_fixture(store, credential, conversation)
            wrong = DatabaseFinalizer(store, replace(claim, owner=uuid4()))
            assert (await wrong.finalize(prepared)).commit.state == "unknown"
            assert await trip_counts(store, conversation.trip_id) == [1, 0, 0]
            finalizer = DatabaseFinalizer(store, claim)
            # Another writer still holds the commit rows: no receipt is NOT rollback proof.
            async with store.writers.begin() as db:
                await db.scalar(
                    select(ChatConversation)
                    .where(ChatConversation.conversation_id == conversation.conversation_id)
                    .with_for_update()
                )
                unknown = await finalizer.reconcile(prepared)
                assert unknown.commit.state == "unknown" and not unknown.commit.error.retryable
            async with store.writers() as db:
                run_row = await db.get(ChatRun, claim.context.identity.run_id)
                assert run_row.response is None
            resolved = await finalizer.reconcile(prepared)
            assert resolved.commit.state == "not_applied"
            assert await trip_counts(store, conversation.trip_id) == [1, 0, 0]
            replay = await store.admit(credential, conversation.conversation_id, request)
            assert replay.response.status == "failed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_admission_has_one_owner_across_store_instances(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        store = ChatV2Store(write_session_factory(engine))
        other = ChatV2Store(write_session_factory(engine))
        try:
            credential, conversation = await bootstrap(store)
            request = request_for(conversation)
            results = await asyncio.gather(
                store.admit(credential, conversation.conversation_id, request),
                other.admit(credential, conversation.conversation_id, request),
            )
            assert sum(isinstance(r, ExecutionClaim) for r in results) == 1
            assert sum(isinstance(r, ExecutionView) for r in results) == 1
            async with store.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRun)) == 1
            # A later request never takes over an occupied run, including after timeout.
            now = store.clock()
            store.clock = lambda: now + timedelta(minutes=3)
            assert (
                await store.admit(credential, conversation.conversation_id, request)
            ).status == "running"
            with pytest.raises(StorageRejected, match="RUN_ACTIVE"):
                await store.admit(
                    credential, conversation.conversation_id, request_for(conversation)
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
