"""Multi-request chat uses real commit/replay/context services and an isolated DB."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import ChatSession
from app.modules.ai.interpretation import Interpretation
from tests.api.test_chat_tasks import info
from tests.fixtures.state_inspection import origin_draft, trip_state
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


async def start(env):
    original = body()
    initial = (await post(env, R01, original)).json()["data"]
    return following(original, initial), initial


async def turn(env, current, message, *, selected=None, **fields):
    env.interpretation = Interpretation(**({"intent": "recommendation"} | fields))
    value = chat(current, message=message, target=selected)
    return (await post(env, C01, value)).json()["data"], value


@pytest.mark.parametrize("legacy", [False, True])
def test_reason_and_change_commit_once_and_explain_original_card(normalized_database, legacy):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            data, sent = await turn(
                env,
                current,
                "왜 추천했어? 그리고 4명, 20분으로",
                selected=target(initial),
                headcount=4,
                max_travel_seconds=1200,
                intent="recommendation_reason" if legacy else "recommendation",
                information_requests=[]
                if legacy
                else [info("recommendation_reason", "왜 추천했어?")],
                recommendation_requested=True,
                recommendation_quote="4명, 20분으로",
            )
            assert data["kind"] == "recommendation"
            assert data["condition_update"]["conditions"]["headcount"] == 4
            assert data["condition_update"]["conditions"]["max_travel_seconds"] == 1200
            assert data["condition_update"]["version"] == 1
            answer = data["task_results"][0]["answer"]
            assert answer["original_result_id"] == initial["result"]["result_id"]
            assert "인원 2명" in answer["text"]
            assert data["clarification"] is None and data["task_clarification"] is None
            before = await counts(env)
            calls = interpretation_count(env), env.source.search.await_count
            assert (await post(env, C01, sent)).json()["data"] == data
            assert await counts(env) == before == (1, 2, 2)
            assert calls == (interpretation_count(env), env.source.search.await_count)

    asyncio.run(run())


@pytest.mark.parametrize("intent", ["recommendation", "scope_notice"])
def test_cancel_change_still_answers_the_requested_reason(normalized_database, intent):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            await turn(
                env,
                current,
                "인원 확인하고 20분으로",
                intent="clarification",
                clarification_fields=["headcount"],
                max_travel_seconds=1200,
            )
            data, _ = await turn(
                env,
                current,
                "변경은 취소하고 이유만 알려줘",
                intent=intent,
                selected=target(initial),
                dialogue_action="cancel",
                action_quote="변경은 취소",
                information_requests=[info("recommendation_reason", "이유만 알려줘")],
            )
            assert not data["condition_update"]["applied"]
            assert data["task_results"][0]["status"] == "completed"
            assert (await trip_state(env.app, current["trip_id"])).condition_draft is None
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())


def test_new_result_question_needs_selection_from_that_result(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            changed, _ = await turn(
                env,
                current,
                "4명으로 바꾸고 새 추천 이유 알려줘",
                headcount=4,
                selected=target(initial),
                information_requests=[info("recommendation_reason", "새 추천 이유", "new_result")],
            )
            assert changed["task_results"][0]["status"] == "needs_input"
            current = following(current, changed)
            old, _ = await turn(
                env,
                current,
                "이 카드로 이어서",
                selected=target(initial),
                resume_information=True,
                resume_quote="이 카드로 이어서",
            )
            assert old["task_results"][0]["status"] == "needs_input"
            new, _ = await turn(
                env,
                current,
                "이 카드로 이어서",
                selected=target(changed),
                resume_information=True,
                resume_quote="이 카드로 이어서",
            )
            assert (
                new["task_results"][0]["answer"]["original_result_id"]
                == (changed["result"]["result_id"])
            )
            assert "인원 4명" in new["message"]

    asyncio.run(run())


def test_answer_then_missing_count_resumes_only_change(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "왜 추천했어? 인원 바꾸고 20분으로",
                selected=target(initial),
                intent="clarification",
                information_requests=[info("recommendation_reason", "왜 추천했어?")],
                clarification_fields=["headcount"],
                max_travel_seconds=1200,
            )
            assert first["kind"] == "clarification"
            assert first["task_results"][0]["status"] == "completed"
            assert not first["condition_update"]["applied"]
            second, _ = await turn(env, current, "4명", headcount=4)
            assert second["kind"] == "recommendation" and not second["task_results"]
            assert second["condition_update"]["conditions"]["headcount"] == 4
            assert second["condition_update"]["conditions"]["max_travel_seconds"] == 1200
            assert await counts(env) == (1, 2, 2)

    asyncio.run(run())


def test_change_with_ambiguous_question_keeps_original_result_binding(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "다른 카드 이유도 알려주고 4명으로",
                selected=target(initial),
                headcount=4,
                information_requests=[
                    info("recommendation_reason", "다른 카드 이유", "unresolved")
                ],
            )
            assert first["condition_update"]["applied"]
            assert first["task_clarification"]["required_fields"] == ["target"]
            new_current = following(current, first)
            wrong, _ = await turn(
                env,
                new_current,
                "이 카드로 이어서",
                selected=target(first),
                resume_information=True,
                resume_quote="이 카드로 이어서",
            )
            assert wrong["task_results"][0]["status"] == "needs_input"
            right, _ = await turn(
                env,
                new_current,
                "이 카드로 이어서",
                selected=target(initial),
                resume_information=True,
                resume_quote="이 카드로 이어서",
            )
            assert (
                right["task_results"][0]["answer"]["original_result_id"]
                == (initial["result"]["result_id"])
            )
            assert not right["condition_update"]["applied"]
            assert await counts(env) == (1, 2, 2)

    asyncio.run(run())


def test_conditional_request_keeps_patch_until_explicit_proceed(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "조건이 맞으면 이유 알려주고 4명으로",
                selected=target(initial),
                headcount=4,
                information_requests=[info("recommendation_reason", "이유 알려주고")],
                request_relation="conditional",
                relation_quote="조건이 맞으면",
            )
            assert not first["condition_update"]["applied"]
            assert first["clarification"]["pending_patch"]["headcount"] == 4
            assert first["clarification"]["required_fields"] == ["dependency"]
            assert "확정할 수 없어요" in first["message"]
            second, _ = await turn(
                env,
                current,
                "보장 안 돼도 진행해",
                dialogue_action="proceed",
                action_quote="보장 안 돼도 진행해",
            )
            assert second["condition_update"]["conditions"]["headcount"] == 4
            assert not second["task_results"]

    asyncio.run(run())


def test_information_first_waits_for_target_then_applies_saved_change(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "이유 먼저 확인한 다음 4명으로",
                headcount=4,
                information_requests=[info("recommendation_reason", "이유")],
                request_relation="information_first",
                relation_quote="먼저 확인한 다음",
            )
            assert not first["condition_update"]["applied"]
            second, _ = await turn(
                env,
                current,
                "이 카드로 이어서",
                selected=target(initial),
                resume_information=True,
                resume_quote="이 카드로 이어서",
            )
            assert second["task_results"][0]["status"] == "completed"
            assert second["condition_update"]["conditions"]["headcount"] == 4
            assert await counts(env) == (1, 2, 2)

    asyncio.run(run())


def test_recommendation_first_defers_information_until_count_answer(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "인원 바꾼 다음 이 카드 이유 알려줘",
                selected=target(initial),
                intent="clarification",
                clarification_fields=["headcount"],
                information_requests=[info("recommendation_reason", "이 카드 이유")],
                request_relation="recommendation_first",
                relation_quote="바꾼 다음",
            )
            assert first["task_results"][0]["status"] == "waiting"
            second, _ = await turn(env, current, "5명", headcount=5)
            assert second["condition_update"]["conditions"]["headcount"] == 5
            assert (
                second["task_results"][0]["answer"]["original_result_id"]
                == (initial["result"]["result_id"])
            )

    asyncio.run(run())


def test_commit_failure_preserves_draft_and_retry_does_not_lose_change(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            await turn(
                env,
                current,
                "인원 바꾸고 이유 알려줘",
                selected=target(initial),
                intent="clarification",
                clarification_fields=["headcount"],
                information_requests=[info("recommendation_reason", "이유")],
            )
            before = deepcopy(await trip_state(env.app, current["trip_id"]))
            writers = env.app.state.write_session_factory

            async def break_commit(session):
                await session.execute(update(ChatSession).values(condition_version=999))

            env.app.state.write_session_factory = WriteHook(writers, break_commit)
            env.interpretation = Interpretation(intent="recommendation", headcount=4)
            value = chat(current, message="4명")
            failed = (await post(env, C01, value, 503)).json()["data"]
            assert not failed["condition_update"]["applied"]
            assert (
                await trip_state(env.app, current["trip_id"])
            ).condition_draft == before.condition_draft
            env.app.state.write_session_factory = writers
            value["request_id"] = str(uuid4())
            accepted = (await post(env, C01, value)).json()["data"]
            assert accepted["condition_update"]["conditions"]["headcount"] == 4
            assert await counts(env) == (1, 2, 2)

    asyncio.run(run())


def test_origin_confirmation_can_coexist_with_reason_without_losing_its_token(normalized_database):
    from tests.integration.test_backend_integrated_flow import integrated_runtime
    from tests.integration.test_origin_conversation_storage import select_origin, setup_origins

    async def run():
        async with integrated_runtime(normalized_database) as env:
            current, initial = await start(env)
            setup_origins(env)
            first, _ = await turn(
                env,
                current,
                "이유 알려주고 시청역에서 4명으로",
                selected=target(initial),
                origin_name="시청역",
                headcount=4,
                information_requests=[info("recommendation_reason", "이유 알려주고")],
            )
            question = first["clarification"]
            assert first["task_results"][0]["status"] == "completed"
            assert question["origin_context"] and not first["condition_update"]["applied"]
            token = question["origin_context"]["context_token"]
            env.interpretation = Interpretation(
                intent="recommendation_reason",
                information_requests=[info("recommendation_reason", "이유 다시")],
            )
            read = chat(current, message="이유 다시 알려줘", target=target(initial))
            read["pending_clarification"] = question
            answer = (await post(env, C01, read)).json()["data"]
            assert not answer["condition_update"]["applied"]
            assert (await origin_draft(env.app, token)).used_by is None
            selected = (await post(env, C01, select_origin(current, question))).json()["data"]
            assert selected["condition_update"]["conditions"]["headcount"] == 4
            assert (
                selected["condition_update"]["conditions"]["origin"]
                == (question["choices"][0]["value"]["location"])
            )

    asyncio.run(run())


def test_direct_recommendation_invalidates_pending_information_from_old_conditions(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            current, initial = await start(env)
            first, _ = await turn(
                env,
                current,
                "다른 카드 이유 알려줘",
                selected=target(initial),
                information_requests=[
                    info("recommendation_reason", "다른 카드 이유", "unresolved")
                ],
            )
            assert first["task_results"][0]["status"] == "needs_input"
            changed = (
                await post(
                    env,
                    R01,
                    current | {"request_id": str(uuid4()), "conditions_patch": {"headcount": 5}},
                )
            ).json()["data"]
            resumed, _ = await turn(
                env,
                following(current, changed),
                "이어서 알려줘",
                resume_information=True,
                resume_quote="이어서 알려줘",
            )
            assert not resumed["task_results"] and not resumed["condition_update"]["applied"]
            assert "대기 중인 정보 요청이 없어요" in resumed["message"]

    asyncio.run(run())
