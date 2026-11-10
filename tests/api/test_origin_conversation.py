"""C01 origin dialogue through production wiring, with all providers mocked."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import get_recommendation_execution
from app.clients.kakao import KakaoError
from app.modules.ai.interpretation import Interpretation
from app.services.recommendation_persistence import RecommendationPersistence
from tests.api.test_recommendation_integration import context as context
from tests.api.test_recommendation_integration import ports as ports

URL = "/api/v1/chat/messages"


def places(*names):
    return {
        "documents": [
            {
                "id": str(i),
                "place_name": name,
                "x": str(126.97 + i / 1000),
                "y": "37.56",
                "address_name": f"서울 중구 세종대로 {i}",
                "category_group_code": "SW8",
                "category_name": "교통 > 지하철역",
            }
            for i, name in enumerate(names, 1)
        ],
        "meta": {"is_end": True},
    }


@pytest.fixture
def flow(ports):
    source, model, kakao = ports
    original = model.complete.side_effect
    state = SimpleNamespace(
        source=source,
        model=model,
        kakao=kakao,
        payloads=[],
        parsed=Interpretation(intent="recommendation", origin_name="시청역"),
    )

    async def complete(instructions, payload, output_type):
        if output_type is Interpretation:
            state.payloads.append(deepcopy(payload))
            return state.parsed
        return await original(instructions, payload, output_type)

    model.complete.side_effect = complete
    kakao.keyword.return_value = places("시청역", "시청역")
    kakao.address.return_value = places()
    kakao.region.return_value = {
        "documents": [
            {
                "region_type": "B",
                "region_1depth_name": "서울특별시",
                "region_2depth_name": "중구",
                "region_3depth_name": "",
            }
        ]
    }
    return state


def send(client, context, message="시청역 출발", **extra):
    response = client.post(
        URL,
        json=context
        | {
            "request_id": str(uuid4()),
            "message": message,
        }
        | extra,
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def action(question, kind="select", index=0):
    value = {"action": kind, "context_token": question["origin_context"]["context_token"]}
    if kind == "select":
        value["candidate_id"] = question["choices"][index]["value"]["candidate_id"]
    return value


def assert_unapplied(data, context):
    assert data["kind"] == "clarification"
    assert data["condition_update"] == {
        "applied": False,
        "conditions": context["current_conditions"],
        "version": context["condition_version"],
        "changes": [],
    }


def test_pending_conditions_apply_once_with_server_candidate_and_replay(client, context, flow):
    flow.parsed = Interpretation(
        intent="recommendation",
        origin_name="시청역",
        max_travel_seconds=1200,
        headcount=3,
        purposes=[{"purpose": "cafe", "keyword": "디저트"}],
    )
    first = send(client, context, "시청역에서 3명, 20분 안에 디저트 카페")
    assert_unapplied(first, context)
    question = first["clarification"]
    assert question["origin_context"]["mode"] == "choose"
    flow.source.search.assert_not_awaited()
    flow.source.route.assert_not_awaited()
    echoed = deepcopy(question)
    echoed["pending_patch"]["max_travel_seconds"] = 9999
    echoed["choices"][1]["value"]["location"]["coordinates"]["latitude"] = 35.1
    echoed["choices"][1]["value"]["location"]["label"] = "변조된 후보"
    echoed["question"] = "변조된 질문"
    body = context | {
        "request_id": str(uuid4()),
        "message": "두 번째 장소",
        "origin_action": action(question, index=1),
        "pending_clarification": echoed,
    }
    response = client.post(URL, json=body)
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    update = data["condition_update"]
    assert data["kind"] == "recommendation" and update["applied"]
    assert update["version"] == 1
    assert update["conditions"]["origin"] == question["choices"][1]["value"]["location"]
    assert update["conditions"]["max_travel_seconds"] == 1200
    assert update["conditions"]["headcount"] == 3
    assert update["conditions"]["purposes"][0]["keyword"] == "디저트"
    assert flow.source.region.call_args_list[0].args[0].latitude == 37.56
    assert data["stages"]["interpretation"] == "skipped" and len(flow.payloads) == 1
    calls = (list(flow.source.mock_calls), list(flow.model.mock_calls), list(flow.kakao.mock_calls))
    replay = client.post(URL, json=body)
    assert replay.status_code == 200 and replay.json()["data"] == data
    assert calls == (flow.source.mock_calls, flow.model.mock_calls, flow.kakao.mock_calls)


@pytest.mark.parametrize("reply,index", [("2번", 1), ("1번이요", 0)])
def test_pure_number_selects_without_interpretation(client, context, flow, reply, index):
    question = send(client, context)["clarification"]
    data = send(client, context, reply, pending_clarification=question)
    assert data["kind"] == "recommendation" and len(flow.payloads) == 1
    assert (
        data["condition_update"]["conditions"]["origin"]
        == question["choices"][index]["value"]["location"]
    )
    flow.kakao.keyword.assert_awaited_once()


@pytest.mark.parametrize("reply", ["응", "0번", "9번"])
def test_unspecified_or_out_of_range_choice_repeats_question(client, context, flow, reply):
    first = send(client, context)
    repeated = send(client, context, reply, pending_clarification=first["clarification"])
    assert_unapplied(repeated, context)
    assert repeated["clarification"] == first["clarification"]
    assert len(flow.payloads) == 1
    flow.source.search.assert_not_awaited()


def test_single_candidate_still_waits_for_yes(client, context, flow):
    flow.kakao.keyword.return_value = places("시청역")
    first = send(client, context)
    assert_unapplied(first, context)
    assert first["clarification"]["origin_context"]["mode"] == "confirm"
    data = send(client, context, "여기 맞아", pending_clarification=first["clarification"])
    assert data["kind"] == "recommendation" and len(flow.payloads) == 1


def test_headcount_is_not_a_candidate_number_and_ai_sees_only_server_question(
    client, context, flow
):
    question = send(client, context)["clarification"]
    echoed = deepcopy(question)
    echoed["question"] = "변조된 질문"
    echoed["choices"][0]["value"]["location"]["label"] = "변조된 후보"
    echoed["pending_patch"]["max_travel_seconds"] = 9999
    flow.parsed = Interpretation(intent="recommendation", headcount=2)
    data = send(client, context, "2명", pending_clarification=echoed)
    assert_unapplied(data, context)
    patch = data["clarification"]["pending_patch"]
    assert patch == {"headcount": 2}
    payload = flow.payloads[-1]
    assert payload["origin_confirmation"]["mode"] == "choose"
    assert len(payload["origin_confirmation"]["candidates"]) == 2
    assert all(set(c) == {"name", "address"} for c in payload["origin_confirmation"]["candidates"])
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "변조된" not in rendered
    assert question["origin_context"]["context_token"] not in rendered
    assert "candidate_id" not in rendered and "9999" not in rendered
    flow.source.search.assert_not_awaited()


def test_mixed_selection_and_time_change_are_interpreted_once(client, context, flow):
    question = send(client, context)["clarification"]
    flow.parsed = Interpretation(
        intent="recommendation",
        origin_reply={"action": "select", "candidate_index": 2},
        max_travel_seconds=1200,
    )
    data = send(client, context, "2번이고 20분 이내로 바꿔줘", pending_clarification=question)
    assert data["kind"] == "recommendation" and len(flow.payloads) == 2
    assert data["condition_update"]["conditions"]["max_travel_seconds"] == 1200
    assert (
        data["condition_update"]["conditions"]["origin"]
        == question["choices"][1]["value"]["location"]
    )
    flow.kakao.keyword.assert_awaited_once()


def test_alternatives_restart_and_new_search_keep_other_draft_changes(client, context, flow):
    context["current_conditions"]["max_travel_seconds"] = 1800
    flow.kakao.keyword.return_value = places("시청역", "시청역입구")
    flow.parsed = Interpretation(
        intent="recommendation",
        origin_name="시청역",
        headcount=4,
        clear_fields=["max_travel_seconds"],
    )
    first = send(client, context)["clarification"]
    assert first["origin_context"]["mode"] == "confirm"
    listed = send(
        client, context, "다른 후보 보기", origin_action=action(first, "show_alternatives")
    )["clarification"]
    assert listed["origin_context"]["mode"] == "choose"
    assert listed["choices"] == first["choices"]
    assert listed["origin_context"]["expires_at"] == first["origin_context"]["expires_at"]
    restarted = send(client, context, "다시 입력", pending_clarification=listed)["clarification"]
    assert restarted["origin_context"]["mode"] == "refine" and restarted["choices"] == []
    assert restarted["pending_patch"] == {"headcount": 4, "max_travel_seconds": None}
    assert restarted["origin_context"]["expires_at"] == first["origin_context"]["expires_at"]
    assert len(flow.payloads) == 1
    flow.kakao.keyword.assert_awaited_once()
    flow.source.search.assert_not_awaited()
    flow.kakao.keyword.return_value = places("서울역")
    flow.parsed = Interpretation(intent="recommendation", origin_name="서울역")
    searched = send(client, context, "서울역에서 출발", pending_clarification=restarted)[
        "clarification"
    ]
    assert searched["pending_patch"] == restarted["pending_patch"]
    assert searched["origin_context"]["expires_at"] > restarted["origin_context"]["expires_at"]
    data = send(client, context, "네", pending_clarification=searched)
    assert data["condition_update"]["conditions"]["max_travel_seconds"] is None
    assert data["condition_update"]["conditions"]["headcount"] == 4
    assert flow.kakao.keyword.await_count == 2


def test_missing_purpose_after_selection_asks_without_reconfirming_origin(client, context, flow):
    context["current_conditions"]["purposes"] = []
    question = send(client, context)["clarification"]
    waiting = send(client, context, origin_action=action(question, index=1))
    assert_unapplied(waiting, context)
    question = waiting["clarification"]
    assert question["origin_context"]["mode"] == "confirmed"
    assert question["required_fields"] == ["purposes"] and question["choices"] == []
    flow.source.search.assert_not_awaited()
    flow.parsed = Interpretation(intent="recommendation", purposes=[{"purpose": "cafe"}])
    data = send(client, context, "카페", pending_clarification=question)
    assert data["kind"] == "recommendation"
    assert data["condition_update"]["conditions"]["origin"] == question["pending_patch"]["origin"]
    assert flow.payloads[-1]["origin_confirmation"]["mode"] == "confirmed"
    flow.kakao.keyword.assert_awaited_once()


@pytest.mark.parametrize("general_question_first", [False, True])
def test_ambiguous_headcount_remains_pending_after_origin_selection(
    client, context, flow, general_question_first
):
    extra = {}
    if general_question_first:
        # General questions now resume the server draft, not arbitrary client patches.
        flow.parsed = Interpretation(
            intent="clarification", clarification_fields=["headcount"], max_travel_seconds=1200
        )
        extra["pending_clarification"] = send(client, context, "6명 이상, 20분 이내")[
            "clarification"
        ]
        flow.parsed = Interpretation(intent="recommendation", origin_name="시청역")
    else:
        flow.parsed = Interpretation(
            intent="clarification",
            origin_name="시청역",
            question="정확히 몇 명인가요?",
            clarification_fields=["headcount"],
            max_travel_seconds=1200,
        )
    question = send(client, context, "시청역 출발, 6명 이상", **extra)["clarification"]
    waiting = send(client, context, origin_action=action(question))
    assert_unapplied(waiting, context)
    question = waiting["clarification"]
    assert question["required_fields"] == ["headcount"]
    flow.source.search.assert_not_awaited()
    flow.parsed = Interpretation(intent="recommendation", headcount=7)
    data = send(client, context, "7명", pending_clarification=question)
    assert data["kind"] == "recommendation"
    assert data["condition_update"]["conditions"]["headcount"] == 7
    assert data["condition_update"]["conditions"]["max_travel_seconds"] == 1200


@pytest.mark.parametrize("bad", ["candidate", "trip", "conditions"])
def test_invalid_selection_is_rejected_before_model_and_providers(client, context, flow, bad):
    question = send(client, context)["clarification"]
    body = deepcopy(context) | {
        "request_id": str(uuid4()),
        "message": "선택",
        "origin_action": action(question),
    }
    if bad == "candidate":
        body["origin_action"]["candidate_id"] = "unknown"
    elif bad == "trip":
        body["trip_id"] = str(uuid4())
    else:
        body["current_conditions"]["headcount"] = 9
    before = (list(flow.model.mock_calls), list(flow.kakao.mock_calls))
    response = client.post(URL, json=body)
    assert response.status_code == (409 if bad == "conditions" else 422), response.json()
    assert not response.json()["data"]["condition_update"]["applied"]
    assert before == (flow.model.mock_calls, flow.kakao.mock_calls)
    flow.source.search.assert_not_awaited()


def test_same_confirmed_origin_does_not_trigger_search(client, context, flow):
    flow.parsed = Interpretation(intent="recommendation", origin_name="서울", headcount=2)
    data = send(client, context, "같은 출발지에서 두 명")
    assert data["kind"] == "recommendation"
    flow.kakao.keyword.assert_not_awaited()
    flow.kakao.region.assert_not_awaited()


def test_failed_new_search_leaves_previous_choice_usable(client, context, flow):
    question = send(client, context)["clarification"]
    flow.parsed = Interpretation(intent="recommendation", origin_name="서울역")
    flow.kakao.keyword.side_effect = KakaoError(502, "network unavailable")
    response = client.post(
        URL,
        json=context
        | {
            "request_id": str(uuid4()),
            "message": "서울역으로 변경",
            "pending_clarification": question,
        },
    )
    assert response.status_code == 502, response.json()
    assert not response.json()["data"]["condition_update"]["applied"]
    data = send(client, context, origin_action=action(question))
    assert data["kind"] == "recommendation"


def test_failed_commit_does_not_consume_choice(client, app, context, flow):
    question = send(client, context)["clarification"]
    execution = app.dependency_overrides[get_recommendation_execution]()
    persistence = AsyncMock(spec=RecommendationPersistence)
    persistence.commit.side_effect = SQLAlchemyError("commit rolled back")
    execution.persistence = persistence
    response = client.post(
        URL,
        json=context
        | {
            "request_id": str(uuid4()),
            "message": "선택",
            "origin_action": action(question),
        },
    )
    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
    assert not response.json()["data"]["condition_update"]["applied"]
    token = question["origin_context"]["context_token"]
    assert app.state.origin_contexts.entries[token].used_by is None
    assert not app.state.origin_contexts.reserved
    execution.persistence = None
    recovered = send(client, context, origin_action=action(question))
    assert recovered["kind"] == "recommendation"
