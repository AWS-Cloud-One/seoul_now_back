import hashlib
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.dependencies import (
    get_ai_provider,
    get_context_reader,
    get_kakao_local_client,
    get_recommendation_service,
)
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.recommendations import Change
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_context_payload import prepare_snapshot
from app.services.recommendations import RecommendationOutcome
from tests.fixtures.recommendation_context import NOW, response

TOKEN = "A" * 43


@pytest.fixture
def followups(app):
    original = response()
    stored = prepare_snapshot(
        original,
        uuid4(),
        token_digest=hashlib.sha256(TOKEN.encode()).hexdigest(),
        expires_at=NOW + timedelta(minutes=30),
    )
    repo = AsyncMock(spec=RecommendationContextRepository)
    repo.get_by_token_digest.side_effect = lambda digest: (
        stored if digest == stored.result.context_token_digest else None
    )
    clock = [NOW]
    contexts = RecommendationContextService(repo, clock=lambda: clock[0])
    model, kakao, recommendations = AsyncMock(), AsyncMock(), AsyncMock()
    model.complete.return_value = Interpretation(intent="recommendation_reason")
    app.dependency_overrides[get_context_reader] = lambda: contexts
    app.dependency_overrides[get_ai_provider] = lambda: model
    app.dependency_overrides[get_kakao_local_client] = lambda: kakao
    app.dependency_overrides[get_recommendation_service] = lambda: recommendations
    body = {
        "request_id": str(uuid4()),
        "trip_id": str(original.trip_id),
        "condition_version": 7,
        "current_conditions": original.condition_update.conditions.model_dump(mode="json"),
        "excluded_place_ids": ["kakao:original"],
        "message": "왜 추천했어?",
        "target": {
            "result_id": original.result["result_id"],
            "place_id": "kakao:original",
            "context_token": TOKEN,
        },
    }
    body["current_conditions"]["headcount"] = 4
    return body, repo, model, kakao, recommendations, clock


def assert_no_new_recommendation(followups):
    _, repo, _, kakao, recommendations, _ = followups
    repo.save.assert_not_awaited()
    kakao.keyword.assert_not_awaited()
    recommendations.execute.assert_not_awaited()


def assert_preserved(data, body):
    assert data["condition_update"]["applied"] is False
    assert data["condition_update"]["conditions"] == body["current_conditions"]
    assert data["condition_update"]["version"] == body["condition_version"]
    assert data["condition_update"]["changes"] == []
    assert data["result"] is None


def test_reason_question_uses_one_interpretation_and_one_context_read(client, followups):
    body, repo, model, _, _, _ = followups
    body["pending_clarification"] = {
        "question": "출발지?",
        "required_fields": ["origin"],
        "pending_patch": {"headcount": 99},
        "choices": [],
    }
    before = deepcopy(body)
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["kind"] == "answer" and data["answer"]["topic"] == "recommendation_reason"
    assert "인원 2명" in data["message"]
    assert data["answer"]["checked_at"] is None
    assert data["stages"] == {"interpretation": "ok", "routes": "skipped", "explanation": "skipped"}
    assert_preserved(data, body)
    assert body == before
    model.complete.assert_awaited_once()
    _, payload, output = model.complete.call_args.args
    assert output is Interpretation
    assert payload["selected_place"] == {"name": "당시 카페", "kind": "recommendation"}
    assert TOKEN not in repr(payload) and body["target"]["result_id"] not in repr(payload)
    repo.get_by_token_digest.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize(
    "fault,status,code",
    [
        ("expired", 410, "CONTEXT_EXPIRED"),
        ("token", 422, "INVALID_CONTEXT"),
        ("place", 422, "INVALID_CONTEXT"),
        ("trip", 422, "INVALID_CONTEXT"),
        ("result", 422, "INVALID_CONTEXT"),
    ],
)
def test_invalid_target_stops_before_any_model_call(client, followups, fault, status, code):
    body, _, model, _, _, clock = followups
    if fault == "expired":
        clock[0] = NOW + timedelta(minutes=30)
    elif fault == "token":
        body["target"]["context_token"] = "B" * 43
    elif fault == "place":
        body["target"]["place_id"] = "kakao:other"
    elif fault == "trip":
        body["trip_id"] = str(uuid4())
    else:
        body["target"]["result_id"] = str(uuid4())
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == status and result.json()["error"]["code"] == code
    assert_preserved(result.json()["data"], body)
    assert result.json()["data"]["stages"]["interpretation"] == "skipped"
    model.complete.assert_not_awaited()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize(
    "intent", ["recommendation_reason", "travel_time", "parking", "population"]
)
def test_followup_without_target_asks_for_card_even_with_missing_current_conditions(
    client, followups, intent
):
    body, repo, model, _, _, _ = followups
    body.pop("target")
    body["current_conditions"]["origin"] = None
    body["current_conditions"]["purposes"] = []
    model.complete.return_value = Interpretation(intent=intent)
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["kind"] == "clarification"
    assert data["clarification"]["required_fields"] == ["target"]
    assert data["clarification"]["pending_patch"] == {}
    assert_preserved(data, body)
    repo.get_by_token_digest.assert_not_awaited()
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)


def test_target_selection_clarification_can_be_sent_back_unchanged(client, followups):
    body, repo, model, _, _, _ = followups
    target = body.pop("target")
    first = client.post("/api/v1/chat/messages", json=body).json()["data"]
    assert first["kind"] == "clarification"
    body.update(target=target, pending_clarification=first["clarification"], message="이 카드")
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200 and result.json()["data"]["kind"] == "answer"
    assert model.complete.await_count == 2  # Once per request, including the selection response.
    repo.get_by_token_digest.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("intent", ["travel_time", "parking", "population"])
def test_current_fact_question_stays_unconnected_and_never_reuses_old_values_as_current(
    client, followups, intent
):
    body, _, model, _, _, _ = followups
    model.complete.return_value = Interpretation(intent=intent)
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 503 and result.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert result.json()["data"]["answer"] is None
    assert result.json()["data"]["stages"]["interpretation"] == "ok"
    assert_preserved(result.json()["data"], body)
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize(
    "parsed,field",
    [
        (Interpretation(intent="recommendation_reason", target_ambiguous=True), "target"),
        (Interpretation(intent="recommendation_reason", headcount=3), "intent"),
        (Interpretation(intent="intent_clarification"), "intent"),
    ],
)
def test_ambiguous_target_or_mixed_operations_require_confirmation(
    client, followups, parsed, field
):
    body, _, model, _, _, _ = followups
    model.complete.return_value = parsed
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["clarification"]["required_fields"] == [field]
    assert data["clarification"]["pending_patch"] == {}
    assert_preserved(data, body)
    assert_no_new_recommendation(followups)


def test_expiry_during_interpretation_blocks_reason_without_another_db_read(client, followups):
    body, repo, model, _, _, clock = followups

    async def complete(*args):
        clock[0] = NOW + timedelta(minutes=30)
        return Interpretation(intent="recommendation_reason")

    model.complete.side_effect = complete
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 410 and result.json()["error"]["code"] == "CONTEXT_EXPIRED"
    assert result.json()["data"]["stages"]["interpretation"] == "ok"
    repo.get_by_token_digest.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("invalid", [True, False])
def test_interpretation_failure_is_safe_and_never_falls_through_to_recommendation(
    client, followups, invalid
):
    body, _, model, _, _, _ = followups
    if invalid:
        model.complete.return_value = {"intent": "invented-intent", "private": "provider-details"}
    else:
        model.complete.side_effect = ServiceError("PROVIDER_TIMEOUT", "처리 시간 초과")
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == (502 if invalid else 504)
    assert result.json()["data"]["stages"]["interpretation"] == "failed"
    assert "provider-details" not in result.text
    assert_preserved(result.json()["data"], body)
    assert_no_new_recommendation(followups)


def test_condition_clarification_round_trip_preserves_explicit_clear_and_applies_new_answer_once(
    client, followups
):
    body, _, model, _, recommendations, _ = followups
    body.pop("target")
    model.complete.return_value = Interpretation(
        intent="clarification", clear_fields=["headcount"], question="어떤 목적으로 갈까요?"
    )
    first = client.post("/api/v1/chat/messages", json=body).json()["data"]
    assert first["clarification"]["pending_patch"] == {"headcount": None}
    assert_preserved(first, body)
    body.update(
        pending_clarification=first["clarification"], message="카페", request_id=str(uuid4())
    )
    model.complete.return_value = Interpretation(intent="recommendation")

    async def execute(request):
        assert request.conditions_patch.model_dump(exclude_unset=True) == {"headcount": None}
        data = response()
        data.trip_id = request.trip_id
        data.base_condition_version = request.condition_version
        data.condition_update.conditions = request.current_conditions.model_copy(deep=True)
        data.condition_update.conditions.headcount = None
        data.condition_update.version = request.condition_version + 1
        data.condition_update.changes = [Change(field="headcount", before=4, after=None)]
        return RecommendationOutcome(data)

    recommendations.execute.side_effect = execute
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200 and result.json()["data"]["kind"] == "recommendation"
    assert result.json()["data"]["condition_update"]["conditions"]["headcount"] is None
    assert model.complete.await_count == 2
    recommendations.execute.assert_awaited_once()


def test_scope_notice_ignores_pending_condition_changes(client, followups):
    body, _, model, _, _, _ = followups
    model.complete.return_value = Interpretation(intent="scope_notice")
    body["pending_clarification"] = {
        "question": "확인",
        "required_fields": ["origin"],
        "pending_patch": {"headcount": 99},
        "choices": [],
    }
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200 and result.json()["data"]["answer"]["topic"] == "scope_notice"
    assert_preserved(result.json()["data"], body)
    assert_no_new_recommendation(followups)
