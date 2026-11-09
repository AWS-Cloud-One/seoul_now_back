"""Real R01/C01 dependency wiring and migrated storage, with only providers mocked."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.api.dependencies import (
    get_ai_provider,
    get_context_reader,
    get_kakao_local_client,
    get_recommendation_source,
)
from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.main import create_app
from app.models import ChatConditionVersion, ChatSession, RecommendationResult
from app.modules.ai.explanation import ExplanationPlan
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.cards import unavailable_parking
from app.modules.recommendation.progress import MemoryProgressStore
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_context import RecommendationContextService
from tests.api.test_recommendation_integration import candidate
from tests.integration.test_recommendation_execution_storage import WriteHook
from tests.unit.recommendation.test_progress import request

pytestmark = pytest.mark.integration
R01, C01 = "/api/v1/recommendations", "/api/v1/chat/messages"


def body():
    value = request().model_dump(mode="json")
    value["conditions_patch"] = {}
    value["current_conditions"]["headcount"] = 2
    return value


def chat(value, *, message="추천해 줘", target=None):
    result = deepcopy(value)
    result.pop("conditions_patch", None)
    result.update(message=message, request_id=str(uuid4()))
    if target is not None:
        result["target"] = target
    return result


def following(value, data):
    value = deepcopy(value)
    value.update(
        request_id=str(uuid4()),
        condition_version=data["condition_update"]["version"],
        current_conditions=data["condition_update"]["conditions"],
    )
    return value


def target(data):
    result = data["result"]
    return {
        "result_id": result["result_id"],
        "context_token": result["context_token"],
        "place_id": result["cards"][0]["place"]["value"]["place_id"],
    }


def interpretation_count(env):
    return sum(call.args[2] is Interpretation for call in env.model.complete.await_args_list)


@asynccontextmanager
async def runtime(url):
    app = create_app(Settings(_env_file=None, database_url=url, result_context_ttl_seconds=75))
    source, model, kakao = AsyncMock(), AsyncMock(), AsyncMock()
    source.region.return_value = "seoul"
    source.search.side_effect = lambda q, o, purpose, page: (
        [candidate(str(i)) for i in range((page - 1) * 15, page * 15)],
        page == 5,
    )
    source.route.return_value = (600, 1000)
    source.parking.return_value = unavailable_parking()
    env = SimpleNamespace(
        app=app,
        source=source,
        model=model,
        kakao=kakao,
        interpretation=Interpretation(intent="recommendation"),
    )

    async def complete(instructions, payload, output_type):
        if output_type is Interpretation:
            return env.interpretation.model_copy(deep=True)
        assert output_type is ExplanationPlan
        return ExplanationPlan(
            items=[
                {"place_id": item["place_id"], "facts": ["eta"]}
                for item in payload["ordered_candidates"]
            ]
        )

    model.complete.side_effect = complete
    app.dependency_overrides[get_ai_provider] = lambda: model
    app.dependency_overrides[get_recommendation_source] = lambda: source
    app.dependency_overrides[get_kakao_local_client] = lambda: kakao
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            env.client = client
            yield env


async def post(env, path, value, status=200):
    response = await asyncio.wait_for(env.client.post(path, json=value), 5)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response


async def counts(env):
    async with env.app.state.session_factory() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (ChatSession, ChatConditionVersion, RecommendationResult)
            ]
        )


async def expire_context(env, data):
    async with env.app.state.write_session_factory.begin() as session:
        await session.execute(
            update(RecommendationResult)
            .where(RecommendationResult.result_id == UUID(data["result"]["result_id"]))
            .values(
                context_expires_at=RecommendationResult.created_at
                + text("interval '1 microsecond'")
            )
        )


def test_r01_and_c01_share_storage_history_and_return_tokens_for_reason_questions(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            assert first["condition_update"]["version"] == 0
            assert interpretation_count(env) == 0
            result = first["result"]
            assert len(result["context_token"]) == 43
            assert (
                datetime.fromisoformat(result["context_expires_at"])
                - datetime.fromisoformat(result["created_at"])
            ).total_seconds() == 75
            assert (await post(env, R01, original)).json()["data"] == first
            assert env.source.search.await_count == 1

            env.interpretation = Interpretation(intent="recommendation", headcount=4)
            changed_body = chat(following(original, first), message="네 명으로 바꿔 줘")
            changed = (await post(env, C01, changed_body)).json()["data"]
            assert changed["condition_update"]["version"] == 1
            assert changed["condition_update"]["conditions"]["headcount"] == 4
            assert changed["stages"]["interpretation"] == "ok"
            assert changed["result"]["context_token"] != result["context_token"]
            first_ids = {c["place"]["value"]["place_id"] for c in first["result"]["cards"]}
            assert not first_ids.intersection(
                c["place"]["value"]["place_id"] for c in changed["result"]["cards"]
            )
            assert (await post(env, C01, changed_body)).json()["data"] == changed
            assert interpretation_count(env) == 1
            assert await counts(env) == (1, 2, 2)

            env.interpretation = Interpretation(intent="recommendation_reason")
            for saved, expected_headcount in ((first, 2), (changed, 4)):
                question = chat(
                    following(original, changed), message="왜 추천했어?", target=target(saved)
                )
                answer = (await post(env, C01, question)).json()["data"]
                assert (
                    answer["kind"] == "answer"
                    and f"인원 {expected_headcount}명" in answer["message"]
                )
                assert answer["condition_update"]["conditions"]["headcount"] == 4
                assert answer["condition_update"]["version"] == 1
                assert not answer["condition_update"]["applied"]
                assert answer["answer"]["original_result_id"] == saved["result"]["result_id"]
                assert answer["answer"]["information"] == [
                    e["information"] for e in saved["result"]["cards"][0]["reason"]["evidence"]
                ]
            assert await counts(env) == (1, 2, 2)
            assert env.source.search.await_count == 2
            async with env.app.state.session_factory() as session:
                repo = RecommendationConditionsRepository(session)
                assert (await repo.get_version(UUID(original["trip_id"]), 0)).headcount == 2
                assert (await repo.get_current(UUID(original["trip_id"]))).conditions.headcount == 4
                assert await session.scalar(text("SHOW transaction_read_only")) == "on"

    asyncio.run(run())


def test_concurrent_chat_replay_interprets_searches_and_saves_once(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            entered, release = asyncio.Event(), asyncio.Event()
            original_complete = env.model.complete.side_effect

            async def complete(instructions, payload, output_type):
                if output_type is Interpretation:
                    entered.set()
                    await release.wait()
                return await original_complete(instructions, payload, output_type)

            env.model.complete.side_effect = complete
            env.interpretation = Interpretation(intent="recommendation", headcount=3)
            value = chat(body(), message="세 명이야")
            first = asyncio.create_task(post(env, C01, value))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(post(env, C01, deepcopy(value)))
                assert await counts(env) == (0, 0, 0)
                release.set()
                a, b = await asyncio.gather(first, second)
            finally:
                release.set()
                await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
            assert a.json()["data"] == b.json()["data"]
            assert interpretation_count(env) == 1
            assert env.source.search.await_count == 1 and env.source.route.await_count == 15
            assert await counts(env) == (1, 2, 1)
            entry = env.app.state.recommendation_progress.trip(UUID(value["trip_id"]))
            assert len(entry.requests) == 1 and len(entry.excluded) == 3
            assert entry.version == 1

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", [R01, C01])
def test_http_commit_failure_is_unapplied_and_replay_does_not_repeat_work(
    normalized_database, endpoint
):
    async def run():
        async with runtime(normalized_database) as env:
            writers = env.app.state.write_session_factory

            async def break_commit(session):
                await session.execute(update(ChatSession).values(condition_version=999))

            env.app.state.write_session_factory = WriteHook(writers, break_commit)
            value = body() if endpoint == R01 else chat(body())
            failed = await post(env, endpoint, value, 503)
            data = failed.json()["data"]
            assert failed.json()["error"]["code"] == "DATA_UNAVAILABLE"
            assert data["result"] is None and not data["condition_update"]["applied"]
            assert data["condition_update"]["conditions"] == value["current_conditions"]
            assert data["stages"]["interpretation"] == ("ok" if endpoint == C01 else "skipped")
            assert await counts(env) == (0, 0, 0)
            calls = list(env.model.mock_calls), list(env.source.mock_calls)
            replay = await post(env, endpoint, value, 503)
            assert (
                replay.json()["data"] == data and replay.json()["error"] == failed.json()["error"]
            )
            assert calls == (env.model.mock_calls, env.source.mock_calls)
            env.app.state.write_session_factory = writers
            value["request_id"] = str(uuid4())
            recovered = (await post(env, endpoint, value)).json()["data"]
            assert recovered["result"]["cards"][0]["place"]["value"]["place_id"] == "kakao:0"
            assert [call.args[-1] for call in env.source.search.await_args_list] == [1, 1]
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())


def test_chat_provider_failure_persists_accepted_conditions_and_failure_metadata(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            env.interpretation = Interpretation(intent="recommendation", headcount=3)
            env.source.search.side_effect = ServiceError(
                "RATE_LIMITED", "호출 제한", retry_after_seconds=5
            )
            value = chat(body())
            response = await post(env, C01, value, 429)
            data = response.json()["data"]
            assert response.headers["retry-after"] == "5"
            assert data["stages"]["interpretation"] == "ok"
            assert data["condition_update"]["applied"] and data["condition_update"]["version"] == 1
            assert await counts(env) == (1, 2, 0)
            replay = await post(env, C01, value, 429)
            assert replay.json()["data"] == data and replay.headers["retry-after"] == "5"
            assert interpretation_count(env) == 1 and env.source.search.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("intent", ["clarification", "scope_notice"])
def test_read_only_chat_does_not_create_sessions_or_apply_pending_changes(
    normalized_database, intent
):
    async def run():
        async with runtime(normalized_database) as env:
            env.interpretation = Interpretation(intent=intent, clear_fields=["headcount"])
            value = chat(body())
            value["pending_clarification"] = {
                "question": "확인",
                "required_fields": ["headcount"],
                "pending_patch": {"headcount": 99},
                "choices": [],
            }
            first = (await post(env, C01, value)).json()["data"]
            assert not first["condition_update"]["applied"]
            assert first["condition_update"]["conditions"] == value["current_conditions"]
            assert (await post(env, C01, value)).json()["data"] == first
            assert await counts(env) == (0, 0, 0)
            assert interpretation_count(env) == 1
            env.source.search.assert_not_awaited()
            if intent == "clarification":
                assert first["clarification"]["pending_patch"] == {"headcount": None}
                env.interpretation = Interpretation(intent="recommendation")
                value.update(
                    request_id=str(uuid4()),
                    message="확인",
                    pending_clarification=first["clarification"],
                )
                accepted = (await post(env, C01, value)).json()["data"]
                assert accepted["condition_update"]["conditions"]["headcount"] is None
                assert accepted["condition_update"]["version"] == 1
                assert await counts(env) == (1, 2, 1)

    asyncio.run(run())


@pytest.mark.parametrize("state", ["lost", "expired"])
def test_valid_historical_reason_survives_search_state_loss_but_cannot_restart_search(
    normalized_database, state
):
    async def run():
        async with runtime(normalized_database) as env:
            now = [0.0]
            env.app.state.recommendation_progress = MemoryProgressStore(
                ttl_seconds=10, clock=lambda: now[0]
            )
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            if state == "lost":
                env.app.state.recommendation_progress = MemoryProgressStore()
            else:
                now[0] = 11
            env.interpretation = Interpretation(intent="recommendation_reason")
            question = chat(following(original, first), target=target(first))
            reason = (await post(env, C01, question)).json()["data"]
            assert reason["kind"] == "answer" and not reason["condition_update"]["applied"]
            assert (await post(env, C01, question)).json()["data"] == reason
            assert interpretation_count(env) == 1
            env.interpretation = Interpretation(intent="recommendation")
            rejected = await post(env, C01, chat(following(original, first)), 410)
            assert rejected.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert env.source.search.await_count == 1 and await counts(env) == (1, 1, 1)

    asyncio.run(run())


def test_cached_chat_recommendation_revalidates_its_original_target_token(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            env.interpretation = Interpretation(intent="recommendation", headcount=3)
            value = chat(
                following(original, first), message="세 명으로 추천해 줘", target=target(first)
            )
            newer = (await post(env, C01, value)).json()["data"]
            assert newer["result"]["context_token"] != first["result"]["context_token"]
            await expire_context(env, first)
            calls = list(env.model.mock_calls), list(env.source.mock_calls)
            expired = await post(env, C01, value, 410)
            assert expired.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert expired.json()["data"]["result"] is None
            assert calls == (env.model.mock_calls, env.source.mock_calls)
            assert await counts(env) == (1, 2, 2)

    asyncio.run(run())


@pytest.mark.parametrize("provider_failure", [False, True])
def test_target_expiry_during_provider_work_prevents_condition_and_result_commit(
    normalized_database, provider_failure
):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            env.interpretation = Interpretation(intent="recommendation", headcount=3)
            search = env.source.search.side_effect
            clock = [datetime.now(UTC)]

            async def reader():
                async with env.app.state.session_factory() as session:
                    yield RecommendationContextService(
                        RecommendationContextRepository(session), clock=lambda: clock[0]
                    )

            env.app.dependency_overrides[get_context_reader] = reader

            async def expires(*args):
                clock[0] = datetime.fromisoformat(first["result"]["context_expires_at"])
                if provider_failure:
                    raise ServiceError("RATE_LIMITED", "호출 제한")
                return search(*args)

            env.source.search.side_effect = expires
            value = chat(following(original, first), target=target(first))
            expired = await post(env, C01, value, 410)
            assert expired.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert not expired.json()["data"]["condition_update"]["applied"]
            assert expired.json()["data"]["stages"]["interpretation"] == "ok"
            assert await counts(env) == (1, 1, 1)
            entry = env.app.state.recommendation_progress.trip(UUID(value["trip_id"]))
            assert entry.version == 0 and entry.conditions.headcount == 2

    asyncio.run(run())
