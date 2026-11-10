"""C01 language boundaries with controlled extraction, independent of paid model checks."""

# Shared fixtures intentionally have the same names as test arguments.
# ruff: noqa: F811

from copy import deepcopy

import pytest

from app.api.dependencies import get_recommendation_execution
from app.modules.ai.interpretation import HeadcountHint, Interpretation
from app.schemas.recommendations import Clarification, Conditions
from app.services.chat_conditions import ConditionDraft
from tests.api.test_chat_condition_dialogue import dialogue  # noqa: F401
from tests.api.test_chat_followups import (  # noqa: F401
    assert_no_new_recommendation,
    assert_preserved,
    current_readers,
    followups,
)
from tests.api.test_recommendation_integration import context, ports  # noqa: F401
from tests.fixtures.chat_language import CASES, DRAFT

CASES_BY_ID = {case["id"]: case for case in CASES}
C01 = "/api/v1/chat/messages"


@pytest.mark.parametrize("case_id", ["reason", "population"])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_information_does_not_reask_echoed_conditions(
    client, followups, current_readers, case_id, selected, legacy
):
    body, _, model, *_ = followups
    case = CASES_BY_ID[case_id]
    body.update(message=case["message"])
    body["current_conditions"].update(origin=None, purposes=[])
    if not selected:
        body.pop("target")
    model.complete.return_value = Interpretation(
        intent=case["topic"],
        information_requests=[]
        if legacy
        else [{"topic": case["topic"], "target": "selected", "quote": case["message"]}],
        clarification_fields=["origin", "purposes", "headcount"],
    )
    response = client.post(C01, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert_preserved(data, body)
    assert data["task_results"][0]["topic"] == case["topic"]
    if selected:
        assert data["kind"] == "answer" and data["clarification"] is None
        assert data["task_results"][0]["status"] == "completed"
        if case_id == "reason":
            assert "인원 2명" in data["message"]
        else:
            assert "10~20명" in data["message"]
    else:
        assert data["task_clarification"]["required_fields"] == ["target"]
        assert data["task_clarification"]["pending_patch"] == {}
        assert data["clarification"] is None
    assert_no_new_recommendation(followups)


def test_population_read_pauses_but_keeps_unanswered_headcount_draft(
    app, client, followups, current_readers
):
    body, _, model, *_ = followups
    store = app.dependency_overrides[get_recommendation_execution]().store
    draft = ConditionDraft(
        version=body["condition_version"],
        conditions=Conditions.model_validate(body["current_conditions"]),
        clarification=Clarification.model_validate(DRAFT["clarification"]),
        headcount_hint=HeadcountHint.model_validate(DRAFT["headcount_hint"]),
    )
    store.trips[body["trip_id"]].condition_draft = draft
    case = CASES_BY_ID["pending_population"]
    body.update(message=case["message"], recent_messages=case["recent_messages"])
    model.complete.return_value = Interpretation(
        intent="population",
        information_requests=[{"topic": "population", "quote": case["message"]}],
        clarification_fields=["headcount"],
    )
    response = client.post(C01, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["kind"] == "answer" and data["clarification"] is None
    assert data["pending_update"] == "clear"
    after = store.trips[body["trip_id"]].condition_draft
    assert after.paused and after.unanswered_turns == 1
    assert (
        after.clarification == draft.clarification and after.headcount_hint == draft.headcount_hint
    )
    assert_preserved(data, body)
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("explicit_request", [False, True])
def test_actual_mixed_request_still_asks_only_missing_headcount(
    client, followups, explicit_request
):
    body, _, model, *_ = followups
    body["message"] = "왜 추천했어? 인원도 바꿔줘"
    model.complete.return_value = Interpretation(
        intent="clarification",
        information_requests=[{"topic": "recommendation_reason", "quote": "왜 추천했어?"}],
        recommendation_requested=explicit_request,
        recommendation_quote="인원도 바꿔줘" if explicit_request else None,
        clarification_fields=["headcount"],
    )
    response = client.post(C01, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["task_results"][0]["status"] == "completed"
    assert "인원 2명" in data["task_results"][0]["answer"]["text"]
    assert data["clarification"]["required_fields"] == ["headcount"]
    assert_preserved(data, body)
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("case_id", ["different_reason", "indoor"])
def test_information_scope_and_other_card_do_not_use_selected_population(
    client, followups, current_readers, case_id
):
    body, _, model, *_ = followups
    case = CASES_BY_ID[case_id]
    body["message"] = case["message"]
    model.complete.return_value = Interpretation(
        intent=case["topic"],
        information_requests=[
            {
                "topic": case["topic"],
                "target": case.get("target", "selected"),
                "quote": case["message"],
            }
        ],
    )
    response = client.post(C01, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    if case_id == "different_reason":
        assert data["task_clarification"]["required_fields"] == ["target"]
        assert data["task_results"][0]["answer"] is None
    else:
        assert data["kind"] == "answer" and data["answer"]["topic"] == "scope_notice"
    current_readers.areas.population_at.assert_not_awaited()
    assert_preserved(data, body)
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize(
    "different_purpose,keyword", [(False, None), (True, None), (False, "성수동")]
)
def test_cafe_request_keeps_confirmed_origin_count_and_time(dialogue, different_purpose, keyword):
    before = deepcopy(dialogue.base["current_conditions"])
    before.update(headcount=2, max_travel_seconds=1800)
    if different_purpose:
        before["purposes"] = [{"purpose": "food", "keyword": None}]
    dialogue.base["current_conditions"] = deepcopy(before)
    case = CASES_BY_ID["destination" if keyword else "keep"]
    purpose = {"purpose": "cafe", "keyword": keyword}
    data = dialogue.send(
        case["message"],
        purposes=[purpose],
        recommendation_requested=True,
        recommendation_quote=case["message"],
    )
    assert data["kind"] == "recommendation" and data["clarification"] is None
    after = data["condition_update"]["conditions"]
    assert after == before | {"purposes": [purpose]}
    if keyword:
        assert all(keyword in call.args[0] for call in dialogue.source.search.await_args_list)


def test_two_recommendations_question_never_applies_stray_model_fields(client, followups):
    body, _, model, *_ = followups
    body["message"] = "4명 카페랑 6명 식당을 각각 추천해줘"
    model.complete.return_value = Interpretation(
        intent="intent_clarification",
        purposes=[{"purpose": "cafe"}, {"purpose": "food"}],
        question="어느 추천부터 할까요?",
    )
    response = client.post(C01, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["clarification"]["required_fields"] == ["intent"]
    assert data["clarification"]["pending_patch"] == {}
    assert_preserved(data, body)
    assert_no_new_recommendation(followups)


def test_new_recommendation_does_not_require_a_previous_target(dialogue):
    data = dialogue.send(CASES_BY_ID["keep_other"]["message"], target_ambiguous=True)
    assert data["kind"] == "recommendation" and data["clarification"] is None


def test_missing_origin_is_asked_by_server_even_when_model_omits_question(dialogue):
    dialogue.base["current_conditions"].update(origin=None, purposes=[])
    data = dialogue.send(
        CASES_BY_ID["missing_origin"]["message"],
        purposes=[{"purpose": "cafe", "keyword": None}],
    )
    assert data["kind"] == "clarification"
    assert data["clarification"]["required_fields"] == ["origin"]
    assert data["clarification"]["pending_patch"] == {"purposes": [{"purpose": "cafe"}]}
    dialogue.source.search.assert_not_awaited()
