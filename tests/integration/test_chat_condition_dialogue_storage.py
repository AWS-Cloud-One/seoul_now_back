"""Drafts commit with the existing request boundary; only providers are mocked."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import ChatSession
from app.modules.ai.interpretation import Interpretation
from tests.fixtures.state_inspection import trip_state
from tests.integration.test_recommendation_chat_api_storage import (
    C01,
    R01,
    body,
    chat,
    counts,
    following,
    interpretation_count,
    post,
    runtime,
    target,
)
from tests.integration.test_recommendation_execution_storage import WriteHook

pytestmark = pytest.mark.integration


async def pending(env, original):
    env.interpretation = Interpretation(
        intent="clarification",
        clarification_fields=["headcount"],
        max_travel_seconds=1200,
        headcount_hint={"minimum": 6, "quote": "6명 이상"},
    )
    return (await post(env, C01, chat(original, message="6명 이상이고 20분 이내로"))).json()["data"]


def test_commit_failure_keeps_draft_and_successful_replay_does_not_apply_twice(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            await pending(env, original)
            assert await counts(env) == (0, 0, 0)
            before = deepcopy((await trip_state(env.app, original["trip_id"])).condition_draft)
            writers = env.app.state.write_session_factory

            async def break_commit(session):
                await session.execute(update(ChatSession).values(condition_version=999))

            env.app.state.write_session_factory = WriteHook(writers, break_commit)
            env.interpretation = Interpretation(intent="recommendation", headcount=8)
            value = chat(original, message="8명이야")
            failed = (await post(env, C01, value, 503)).json()["data"]
            assert not failed["condition_update"]["applied"]
            assert await counts(env) == (0, 0, 0)
            assert (await trip_state(env.app, original["trip_id"])).condition_draft == before
            env.app.state.write_session_factory = writers
            value["request_id"] = str(uuid4())
            accepted = (await post(env, C01, value)).json()["data"]
            assert accepted["condition_update"]["conditions"]["max_travel_seconds"] == 1200
            assert accepted["condition_update"]["conditions"]["headcount"] == 8
            assert (await trip_state(env.app, original["trip_id"])).condition_draft is None
            count = await counts(env)
            calls = interpretation_count(env), env.source.search.await_count
            assert (await post(env, C01, value)).json()["data"] == accepted
            assert await counts(env) == count
            assert (interpretation_count(env), env.source.search.await_count) == calls

    asyncio.run(run())


def test_direct_recommendation_invalidates_old_draft_and_old_echo_cannot_restore_it(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            question = (await pending(env, original))["clarification"]
            result = (
                await post(env, R01, original | {"conditions_patch": {"headcount": 3}})
            ).json()["data"]
            assert (await trip_state(env.app, original["trip_id"])).condition_draft is None
            env.interpretation = Interpretation(intent="recommendation")
            value = chat(following(original, result)) | {"pending_clarification": question}
            accepted = (await post(env, C01, value)).json()["data"]
            assert accepted["condition_update"]["conditions"]["headcount"] == 3
            assert accepted["condition_update"]["conditions"]["max_travel_seconds"] is None
            stale = (await post(env, C01, chat(original, message="예전 조건으로"), 409)).json()
            assert stale["error"]["code"] == "CONTEXT_CONDITIONS_MISMATCH"

    asyncio.run(run())


def test_targeted_reason_can_interrupt_question_and_later_resume_same_draft(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            original = body()
            result = (await post(env, R01, original)).json()["data"]
            current = following(original, result)
            await pending(env, current)
            env.interpretation = Interpretation(intent="recommendation_reason")
            info = chat(current, message="왜 추천했어?", target=target(result))
            answer = (await post(env, C01, info)).json()["data"]
            assert answer["kind"] == "answer" and answer["pending_update"] == "clear"
            assert not answer["condition_update"]["applied"]
            draft = (await trip_state(env.app, original["trip_id"])).condition_draft
            assert draft.paused and draft.headcount_hint.minimum == 6
            env.interpretation = Interpretation(intent="recommendation", headcount=8)
            accepted = (await post(env, C01, chat(current, message="아까 인원은 8명"))).json()[
                "data"
            ]
            assert accepted["condition_update"]["conditions"]["headcount"] == 8
            assert accepted["condition_update"]["conditions"]["max_travel_seconds"] == 1200

    asyncio.run(run())
