import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import (
    get_ai_provider,
    get_area_query_service,
    get_context_reader,
    get_kakao_local_client,
    get_place_detail_reader,
    get_recommendation_execution,
    get_recommendation_service,
    get_travel_time_reader,
)
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.progress import MemoryProgressStore, TripProgress
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.location import Coordinates
from app.schemas.pending import PlaceDetailData, TravelTimeData
from app.schemas.recommendations import Change, Conditions
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_context_payload import prepare_snapshot
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendation_persistence import RecommendationPersistence
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
    stored = replace(stored, card_coordinates=(Coordinates(latitude=37.57, longitude=126.98),))
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
    store = MemoryProgressStore()
    store.trips[body["trip_id"]] = TripProgress(
        store.clock() + store.ttl_seconds,
        version=7,
        conditions=Conditions.model_validate(body["current_conditions"]),
    )
    execution = RecommendationExecution(store)
    app.dependency_overrides[get_recommendation_execution] = lambda: execution
    return body, repo, model, kakao, recommendations, clock


@pytest.fixture
def current_readers(app, followups):
    body = followups[0]
    original = response().result["cards"][0]
    places, travel, areas = AsyncMock(), AsyncMock(), AsyncMock()
    places.detail.return_value = PlaceDetailData(
        result_id=body["target"]["result_id"],
        place=original["place"],
        parking=original["parking"] | {"total_count": 7, "nearest_distance_meters": 120},
        original_recommended_at=NOW,
        checked_at=NOW + timedelta(seconds=10),
    )
    travel.check.return_value = TravelTimeData(
        trip_id=body["trip_id"],
        base_condition_version=7,
        result_id=body["target"]["result_id"],
        place_id="kakao:original",
        original_recommended_at=NOW,
        checked_at=NOW + timedelta(seconds=10),
        route=original["route"]
        | {
            "value": {"duration_seconds": 905, "distance_meters": 2222},
            "collected_at": NOW + timedelta(seconds=10),
        },
    )
    areas.population_at.return_value = {
        "checked_at": NOW + timedelta(seconds=10),
        "reason_code": None,
        "areas": [
            {
                "area_code": "POI001",
                "name": "관측 구역",
                "boundary_version": "v1",
                "population": {
                    "value": {
                        "min_people": 10,
                        "max_people": 20,
                        "congestion": "normal",
                        "is_replacement": False,
                    },
                    "status": "available",
                    "freshness": "fresh",
                    "observed_at": NOW,
                    "collected_at": NOW,
                    "source": "seoul_city_population",
                    "reason_code": None,
                },
            }
        ],
    }
    app.dependency_overrides[get_place_detail_reader] = lambda: places
    app.dependency_overrides[get_travel_time_reader] = lambda: travel
    app.dependency_overrides[get_area_query_service] = lambda: areas
    return SimpleNamespace(places=places, travel=travel, areas=areas)


def assert_no_new_recommendation(followups):
    _, repo, _, kakao, recommendations, _ = followups
    repo.save.assert_not_awaited()
    kakao.keyword.assert_not_awaited()
    recommendations.execute.assert_not_awaited()
    recommendations.prepare.assert_not_awaited()


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
    body.update(
        target=target,
        pending_clarification=first["clarification"],
        message="이 카드",
        request_id=str(uuid4()),
    )
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200 and result.json()["data"]["kind"] == "answer"
    assert model.complete.await_count == 2  # Once per request, including the selection response.
    repo.get_by_token_digest.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("intent", ["travel_time", "parking", "population"])
def test_current_fact_question_uses_shared_readers_without_new_recommendation(
    client, followups, current_readers, intent
):
    body, _, model, _, _, _ = followups
    model.complete.return_value = Interpretation(intent=intent)
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["kind"] == "answer" and data["answer"]["topic"] == intent
    fact = data["answer"]["information"][0]
    assert_preserved(data, body)
    if intent == "travel_time":
        assert fact["value"]["duration_seconds"] == 905
        assert data["stages"]["routes"] == "ok"
        current_readers.travel.check.assert_awaited_once()
        current_readers.places.detail.assert_not_awaited()
    elif intent == "parking":
        assert fact["value"]["total_count"] == 7
        assert fact["value"]["availability"] == "unsupported"
        assert fact["collected_at"] is None and fact["observed_at"] is None
        current_readers.places.detail.assert_awaited_once()
        current_readers.travel.check.assert_not_awaited()
    else:
        assert fact["value"]["area_code"] == "POI001"
        assert fact["value"]["population"]["min_people"] == 10
        current_readers.areas.population_at.assert_awaited_once_with(
            Coordinates(latitude=37.57, longitude=126.98),
        )
        current_readers.travel.check.assert_not_awaited()
        current_readers.places.detail.assert_not_awaited()
    replay = client.post("/api/v1/chat/messages", json=body)
    assert replay.json()["data"] == data
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("intent", ["travel_time", "parking", "population"])
def test_current_lookup_failure_preserves_conditions_and_replays_once(
    client, followups, current_readers, intent
):
    body, _, model, _, _, _ = followups
    model.complete.return_value = Interpretation(intent=intent)
    reader = {
        "travel_time": current_readers.travel.check,
        "parking": current_readers.places.detail,
        "population": current_readers.areas.population_at,
    }[intent]
    reader.side_effect = ServiceError("DATA_UNAVAILABLE", "조회 실패")
    for _ in range(2):
        result = client.post("/api/v1/chat/messages", json=body)
        assert result.status_code == 503 and result.json()["data"]["kind"] == "failure"
        assert_preserved(result.json()["data"], body)
    reader.assert_awaited_once()
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize("intent", ["travel_time", "parking", "population"])
def test_expiry_during_current_lookup_blocks_answer(client, followups, current_readers, intent):
    body, _, model, _, _, clock = followups
    model.complete.return_value = Interpretation(intent=intent)
    reader = {
        "travel_time": current_readers.travel.check,
        "parking": current_readers.places.detail,
        "population": current_readers.areas.population_at,
    }[intent]

    async def expire(*args):
        clock[0] = NOW + timedelta(minutes=30)
        return reader.return_value

    reader.side_effect = expire
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 410 and result.json()["error"]["code"] == "CONTEXT_EXPIRED"
    assert_preserved(result.json()["data"], body)
    assert_no_new_recommendation(followups)


@pytest.mark.parametrize(
    "parsed,field",
    [
        (Interpretation(intent="recommendation_reason", target_ambiguous=True), "target"),
        (Interpretation(intent="intent_clarification"), "intent"),
    ],
)
def test_ambiguous_target_or_unspecified_intent_requires_confirmation(
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

    async def prepare(request, work):
        assert request.conditions_patch.model_dump(exclude_unset=True) == {"headcount": None}
        data = response()
        data.trip_id = request.trip_id
        data.base_condition_version = request.condition_version
        data.condition_update.conditions = request.current_conditions.model_copy(deep=True)
        data.condition_update.conditions.headcount = None
        data.condition_update.version = request.condition_version + 1
        data.condition_update.changes = [Change(field="headcount", before=4, after=None)]
        return RecommendationOutcome(data)

    recommendations.prepare.side_effect = prepare
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 200 and result.json()["data"]["kind"] == "recommendation"
    assert result.json()["data"]["condition_update"]["conditions"]["headcount"] is None
    assert model.complete.await_count == 2
    recommendations.prepare.assert_awaited_once()
    recommendations.execute.assert_not_awaited()


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


@pytest.mark.parametrize("changed", ["message", "pending_null", "target", "operation"])
def test_same_request_id_with_different_original_input_conflicts_before_interpretation(
    client, followups, changed
):
    body, repo, model, _, _, _ = followups
    body["pending_clarification"] = {
        "question": "확인",
        "required_fields": ["headcount"],
        "pending_patch": {},
        "choices": [],
    }
    first = client.post("/api/v1/chat/messages", json=body)
    assert first.status_code == 200
    replay = client.post("/api/v1/chat/messages", json=body)
    assert replay.json()["data"] == first.json()["data"]
    altered = deepcopy(body)
    endpoint = "/api/v1/chat/messages"
    if changed == "message":
        altered["message"] = "다른 질문"
    elif changed == "pending_null":
        altered["pending_clarification"]["pending_patch"]["headcount"] = None
    elif changed == "target":
        altered["target"]["place_id"] = "kakao:other"
    else:
        endpoint = "/api/v1/recommendations"
        altered = {
            key: value
            for key, value in body.items()
            if key not in {"message", "target", "pending_clarification"}
        }
        altered["conditions_patch"] = {}
        # Use a real service sharing this test execution owner for the other operation.
        from app.services.recommendations import RecommendationService

        execution = client.app.dependency_overrides[get_recommendation_execution]()
        client.app.dependency_overrides[get_recommendation_service] = lambda: RecommendationService(
            execution=execution
        )
    conflict = client.post(endpoint, json=altered)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
    model.complete.assert_awaited_once()
    assert repo.get_by_token_digest.await_count == 2


@pytest.mark.parametrize("invalid", ["expired", "trip", "result", "place", "token"])
def test_cached_reason_revalidates_expiry_and_all_target_bindings(client, followups, invalid):
    body, repo, model, _, _, clock = followups
    first = client.post("/api/v1/chat/messages", json=body)
    assert first.status_code == 200
    stored = repo.get_by_token_digest.side_effect(hashlib.sha256(TOKEN.encode()).hexdigest())
    if invalid == "expired":
        clock[0] = NOW + timedelta(minutes=30)
    elif invalid == "trip":
        stored.result.trip_id = uuid4()
    elif invalid == "result":
        stored.result.result_id = uuid4()
    elif invalid == "place":
        for card in stored.cards:
            card.place_id = "kakao:other"
    else:
        stored.result.context_token_digest = "b" * 64
    replay = client.post("/api/v1/chat/messages", json=body)
    assert replay.status_code == (410 if invalid == "expired" else 422)
    assert replay.json()["data"]["answer"] is None
    assert replay.json()["data"]["stages"]["interpretation"] == "skipped"
    model.complete.assert_awaited_once()
    assert repo.get_by_token_digest.await_count == 2


@pytest.mark.parametrize("stage", ["interpretation", "db_preflight"])
def test_cached_failure_preserves_the_stage_that_actually_completed(client, followups, stage):
    body, _, model, _, _, _ = followups
    if stage == "interpretation":
        model.complete.side_effect = TimeoutError("private provider detail")
        status, code, interpretation = 504, "PROVIDER_TIMEOUT", "failed"
    else:
        body.pop("target")
        model.complete.return_value = Interpretation(intent="recommendation")
        execution = client.app.dependency_overrides[get_recommendation_execution]()
        execution.persistence = AsyncMock(spec=RecommendationPersistence)
        execution.persistence.check_current.side_effect = SQLAlchemyError("private database detail")
        status, code, interpretation = 503, "DATA_UNAVAILABLE", "ok"
    first = client.post("/api/v1/chat/messages", json=body)
    assert first.status_code == status and first.json()["error"]["code"] == code
    assert first.json()["data"]["stages"]["interpretation"] == interpretation
    assert not first.json()["data"]["condition_update"]["applied"]
    assert "private" not in first.text
    replay = client.post("/api/v1/chat/messages", json=body)
    assert replay.json()["data"] == first.json()["data"]
    assert replay.json()["error"] == first.json()["error"]
    model.complete.assert_awaited_once()
    assert_no_new_recommendation(followups)
