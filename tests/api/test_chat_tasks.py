"""Multi-question reads, isolated provider failures, and scoped continuation."""

# Shared pytest fixtures intentionally have the same names as test arguments.
# ruff: noqa: F811

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from app.api.dependencies import get_recommendation_execution
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from tests.api.test_chat_followups import (  # noqa: F401
    assert_no_new_recommendation,
    current_readers,
    followups,
)


def info(topic, quote, target="selected"):
    return {"topic": topic, "quote": quote, "target": target}


def send(client, body, model, message, **fields):
    model.complete.return_value = Interpretation(**({"intent": "recommendation_reason"} | fields))
    body = deepcopy(body) | {"message": message, "request_id": str(uuid4())}
    response = client.post("/api/v1/chat/messages", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"], body


def test_multiple_reads_and_replay_do_not_recommend(client, followups, current_readers):
    body, _, model, *_ = followups
    data, sent = send(
        client,
        body,
        model,
        "이유랑 주차, 인구 알려줘",
        information_requests=[
            info("recommendation_reason", "이유"),
            info("parking", "주차"),
            info("population", "인구"),
        ],
    )
    assert [item["topic"] for item in data["task_results"]] == [
        "recommendation_reason",
        "parking",
        "population",
    ]
    assert all(item["status"] == "completed" for item in data["task_results"])
    assert "인원 2명" in data["message"] and "7" in data["message"]
    assert not data["condition_update"]["applied"]
    assert client.post("/api/v1/chat/messages", json=sent).json()["data"] == data
    current_readers.places.detail.assert_awaited_once()
    current_readers.areas.population_at.assert_awaited_once()
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)


def test_historical_information_does_not_require_current_search_memory(
    app,
    client,
    followups,
    current_readers,
):
    body, _, model, *_ = followups
    app.dependency_overrides[get_recommendation_execution]().store.trips.clear()
    data, _ = send(
        client,
        body,
        model,
        "이유랑 주차 알려줘",
        information_requests=[info("recommendation_reason", "이유"), info("parking", "주차")],
    )
    assert all(item["status"] == "completed" for item in data["task_results"])
    assert not data["condition_update"]["applied"]
    assert_no_new_recommendation(followups)


def test_failed_read_resumes_alone_on_original_target_without_replaying_reason(
    client,
    followups,
    current_readers,
):
    body, _, model, *_ = followups
    current_readers.places.detail.side_effect = ServiceError("PROVIDER_TIMEOUT", "잠시 지연")
    first, _ = send(
        client,
        body,
        model,
        "이유와 주차 알려줘",
        information_requests=[info("recommendation_reason", "이유"), info("parking", "주차")],
    )
    assert [item["status"] for item in first["task_results"]] == ["completed", "failed"]
    assert first["task_results"][1]["retryable"]
    current_readers.places.detail.side_effect = None
    without_target = deepcopy(body)
    without_target.pop("target")
    retry, _ = send(
        client,
        without_target,
        model,
        "실패한 것만 다시",
        intent="parking",
        resume_information=True,
        resume_quote="실패한 것만 다시",
    )
    assert len(retry["task_results"]) == 1
    assert retry["task_results"][0]["task_id"] == first["task_results"][1]["task_id"]
    assert retry["task_results"][0]["status"] == "completed"
    assert retry["task_results"][0]["answer"]["place_id"] == body["target"]["place_id"]
    assert_no_new_recommendation(followups)


def test_ambiguous_target_uses_server_pending_topic_without_client_echo(
    client,
    followups,
    current_readers,
):
    body, _, model, *_ = followups
    first, _ = send(
        client,
        body,
        model,
        "다른 곳 주차 알려줘",
        information_requests=[info("parking", "다른 곳 주차", "unresolved")],
    )
    assert first["task_clarification"]["required_fields"] == ["target"]
    assert first["task_results"][0]["status"] == "needs_input"
    current_readers.places.detail.assert_not_awaited()
    second, _ = send(
        client,
        body,
        model,
        "이 카드로 이어서 알려줘",
        intent="parking",
        resume_information=True,
        resume_quote="이 카드로 이어서",
    )
    assert second["task_results"][0]["status"] == "completed"
    assert second["task_results"][0]["task_id"] == first["task_results"][0]["task_id"]


def test_scoped_cancel_removes_only_information_without_recommendation(
    client,
    followups,
    current_readers,
):
    body, _, model, *_ = followups
    send(
        client,
        body,
        model,
        "다른 곳 인구 알려줘",
        information_requests=[info("population", "인구", "unresolved")],
    )
    cancelled, _ = send(
        client,
        body,
        model,
        "정보 질문 취소해",
        intent="recommendation",
        dialogue_action="cancel",
        dialogue_scope="information",
        action_quote="정보 질문 취소",
    )
    assert not cancelled["condition_update"]["applied"]
    resumed, _ = send(
        client,
        body,
        model,
        "아까 질문 이어서",
        intent="recommendation",
        resume_information=True,
        resume_quote="아까 질문 이어서",
    )
    assert not resumed["task_results"]
    current_readers.areas.population_at.assert_not_awaited()
    assert_no_new_recommendation(followups)


def test_cached_resumed_answer_revalidates_the_stored_target_expiry(
    client,
    followups,
    current_readers,
):
    body, _, model, _, _, clock = followups
    current_readers.places.detail.side_effect = ServiceError("PROVIDER_TIMEOUT", "지연")
    send(client, body, model, "주차 알려줘", information_requests=[info("parking", "주차")])
    current_readers.places.detail.side_effect = None
    without_target = deepcopy(body)
    without_target.pop("target")
    _, sent = send(
        client,
        without_target,
        model,
        "다시 알려줘",
        intent="parking",
        resume_information=True,
        resume_quote="다시",
    )
    calls = model.complete.await_count
    clock[0] += timedelta(hours=1)
    expired = client.post("/api/v1/chat/messages", json=sent)
    assert expired.status_code == 410
    assert model.complete.await_count == calls


@pytest.mark.parametrize(
    "fields",
    [
        {"information_requests": [info("parking", "말하지 않은 주차 요청")]},
        {"recommendation_requested": True, "recommendation_quote": "4명으로"},
        {"request_relation": "conditional", "relation_quote": "주차 가능하면"},
        {"resume_information": True, "resume_quote": "다시"},
    ],
)
def test_task_controls_need_current_message_evidence(client, followups, fields):
    body, _, model, *_ = followups
    model.complete.return_value = Interpretation(intent="parking", **fields)
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 502
    assert_no_new_recommendation(followups)
