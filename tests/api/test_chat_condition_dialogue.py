"""General clarification journeys through C01; provider outputs are controlled."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.modules.ai.interpretation import Interpretation
from tests.api.test_recommendation_integration import context, ports  # noqa: F401


@pytest.fixture
def dialogue(client, context, ports):  # noqa: F811
    source, model, _ = ports
    explanation = model.complete.side_effect
    env = SimpleNamespace(base=deepcopy(context), pending=None, payloads=[], turns=[])

    async def complete(instructions, payload, output_type):
        if output_type is Interpretation:
            env.payloads.append(deepcopy(payload))
            return env.parsed
        return await explanation(instructions, payload, output_type)

    model.complete.side_effect = complete

    def send(message, *, status=200, echo=True, **parsed):
        env.parsed = Interpretation(**({"intent": "recommendation"} | parsed))
        body = env.base | {
            "request_id": str(uuid4()),
            "message": message,
            "recent_messages": env.turns[-10:],
        }
        if echo and env.pending:
            body["pending_clarification"] = deepcopy(env.pending)
        response = client.post("/api/v1/chat/messages", json=body)
        assert response.status_code == status, response.text
        data = response.json()["data"]
        env.last_body = body
        env.turns += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": data["message"]},
        ]
        if data["condition_update"]["applied"]:
            env.base.update(
                condition_version=data["condition_update"]["version"],
                current_conditions=data["condition_update"]["conditions"],
            )
            env.pending = None
        elif data["kind"] == "clarification":
            env.pending = data["clarification"]
        elif data["pending_update"] == "clear":
            env.pending = None
        return data

    env.send, env.source, env.model, env.client = send, source, model, client
    return env


def ask_headcount(env):
    return env.send(
        "6명 이상이고 30분 이내로 찾아줘",
        intent="clarification",
        headcount_hint={"minimum": 6, "quote": "6명 이상"},
        clarification_fields=["headcount"],
        max_travel_seconds=1800,
    )


def test_unanswered_question_survives_other_changes_without_relying_on_client_echo(dialogue):
    first = ask_headcount(dialogue)
    assert first["clarification"]["required_fields"] == ["headcount"]
    second = dialogue.send("시간은 20분 이내로", max_travel_seconds=1200, echo=False)
    assert second["kind"] == "clarification"
    assert second["clarification"]["required_fields"] == ["headcount"]
    assert second["clarification"]["pending_patch"] == {"max_travel_seconds": 1200}
    assert "인원" in second["message"] and "출발지" not in second["message"]
    assert not second["condition_update"]["applied"]
    dialogue.source.search.assert_not_awaited()
    calls = len(dialogue.payloads)
    replay = dialogue.client.post("/api/v1/chat/messages", json=dialogue.last_body).json()["data"]
    assert replay == second and len(dialogue.payloads) == calls
    third = dialogue.send(
        "8명이야", intent="clarification", headcount=8, clarification_fields=["headcount"]
    )
    assert third["kind"] == "recommendation"
    assert third["condition_update"]["conditions"]["headcount"] == 8
    assert third["condition_update"]["conditions"]["max_travel_seconds"] == 1200
    assert third["condition_update"]["version"] == 1
    assert not any(w["code"] == "HEADCOUNT_BASELINE_ASSUMED" for w in third["warnings"])


def test_optional_questions_resolve_one_at_a_time_and_explicit_clear_is_an_answer(dialogue):
    dialogue.send(
        "인원과 모임 확인", intent="clarification", clarification_fields=["headcount", "group_type"]
    )
    second = dialogue.send("4명이야", headcount=4)
    assert second["clarification"]["required_fields"] == ["group_type"]
    third = dialogue.send("모임 종류는 상관없어", clear_fields=["group_type"])
    assert third["kind"] == "recommendation"
    assert third["condition_update"]["conditions"]["headcount"] == 4
    assert third["condition_update"]["conditions"]["group_type"] is None


def test_pause_and_unrelated_answers_preserve_draft_without_repeating_question(dialogue):
    ask_headcount(dialogue)
    pause = dialogue.send(
        "그건 나중에 정할게", dialogue_action="pause", action_quote="나중에 정할게"
    )
    assert pause["kind"] == "answer" and pause["pending_update"] == "clear"
    for _ in range(3):
        answer = dialogue.send("대중교통도 돼?", intent="scope_notice")
        assert answer["kind"] == "answer" and not answer["condition_update"]["applied"]
    dialogue.source.search.assert_not_awaited()
    assert dialogue.payloads[-1]["condition_dialogue"]["status"] == "paused"
    assert dialogue.payloads[-1]["condition_dialogue"]["headcount_hint"]["minimum"] == 6
    result = dialogue.send("이어서 8명으로 해줘", headcount=8)
    assert result["condition_update"]["conditions"]["max_travel_seconds"] == 1800


def test_cancel_cannot_resurrect_a_client_copy_of_old_draft(dialogue):
    old = ask_headcount(dialogue)["clarification"]
    answer = dialogue.send("아까 변경 취소해", dialogue_action="cancel", action_quote="변경 취소해")
    assert answer["kind"] == "answer" and answer["pending_update"] == "clear"
    assert not answer["condition_update"]["applied"]
    dialogue.pending = old
    result = dialogue.send("현재 조건으로 추천해줘")
    assert dialogue.payloads[-1]["pending_clarification"] is None
    assert result["condition_update"]["conditions"]["headcount"] is None
    assert result["condition_update"]["conditions"]["max_travel_seconds"] is None


def test_restart_drops_only_old_draft_and_uses_new_changes(dialogue):
    ask_headcount(dialogue)
    result = dialogue.send(
        "아까 변경은 잊고 10분 이내로 찾아줘",
        dialogue_action="restart",
        action_quote="아까 변경은 잊고",
        max_travel_seconds=600,
    )
    assert result["kind"] == "recommendation"
    assert result["condition_update"]["conditions"]["headcount"] is None
    assert result["condition_update"]["conditions"]["max_travel_seconds"] == 600


def test_proceed_uses_only_recorded_minimum_and_discloses_the_baseline(dialogue):
    ask_headcount(dialogue)
    dialogue.send("20분 이내로 바꿔줘", max_travel_seconds=1200)
    result = dialogue.send(
        "그 조건으로 일단 찾아줘", dialogue_action="proceed", action_quote="일단 찾아줘"
    )
    assert result["kind"] == "recommendation"
    assert result["condition_update"]["conditions"]["headcount"] == 6
    assert "6명을 추천 기준" in result["message"]
    assert any(w["code"] == "HEADCOUNT_BASELINE_ASSUMED" for w in result["warnings"])


def test_proceed_without_numeric_evidence_does_not_invent_a_headcount(dialogue):
    dialogue.send("여러 명이야", intent="clarification", clarification_fields=["headcount"])
    result = dialogue.send(
        "모르겠고 일단 추천해줘", dialogue_action="proceed", action_quote="일단 추천해줘"
    )
    assert result["condition_update"]["conditions"]["headcount"] is None
    assert any(w["code"] == "UNRESOLVED_OPTIONAL_CHANGES_SKIPPED" for w in result["warnings"])


def test_proceed_never_skips_required_origin(dialogue):
    dialogue.base["current_conditions"]["origin"] = None
    result = dialogue.send("일단 추천해줘", dialogue_action="proceed", action_quote="일단 추천해줘")
    assert result["kind"] == "clarification"
    assert "origin" in result["clarification"]["required_fields"]
    dialogue.source.search.assert_not_awaited()


def test_explicit_clear_removes_old_minimum_before_later_proceed(dialogue):
    ask_headcount(dialogue)
    result = dialogue.send(
        "인원 상관없고 일단 추천해줘",
        dialogue_action="proceed",
        action_quote="일단 추천해줘",
        clear_fields=["headcount"],
    )
    assert result["condition_update"]["conditions"]["headcount"] is None
    assert not any(w["code"] == "HEADCOUNT_BASELINE_ASSUMED" for w in result["warnings"])


def test_a_new_uncertain_headcount_replaces_the_previous_minimum(dialogue):
    ask_headcount(dialogue)
    dialogue.send(
        "아니 인원이 달라져서 아직 몰라",
        intent="clarification",
        clarification_fields=["headcount"],
        headcount_hint={"minimum": None, "quote": "인원이 달라져서 아직 몰라"},
    )
    result = dialogue.send("일단 추천해줘", dialogue_action="proceed", action_quote="일단 추천해줘")
    assert result["condition_update"]["conditions"]["headcount"] is None
    assert not any(w["code"] == "HEADCOUNT_BASELINE_ASSUMED" for w in result["warnings"])


@pytest.mark.parametrize(
    "parsed",
    [
        {"dialogue_action": "cancel", "action_quote": "취소해"},
        {"headcount_hint": {"minimum": 6, "quote": "6명 이상"}},
    ],
)
def test_actions_and_minimums_need_evidence_in_this_user_message(dialogue, parsed):
    result = dialogue.send("20분 이내로", status=502, **parsed)
    assert result["kind"] == "failure" and not result["condition_update"]["applied"]
    dialogue.source.search.assert_not_awaited()
