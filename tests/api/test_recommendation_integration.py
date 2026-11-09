"""Exercise actual R01/C01 wiring with injected I/O; no database or paid calls."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.dependencies import (
    get_ai_provider,
    get_kakao_local_client,
    get_recommendation_execution,
    get_recommendation_source,
)
from app.core.exceptions import ServiceError
from app.modules.ai.explanation import ExplanationPlan
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.cards import unavailable_parking
from app.modules.recommendation.progress import MemoryProgressStore
from app.modules.recommendation.types import Candidate
from app.schemas.pending import Place
from app.schemas.recommendations import Conditions
from app.services.recommendation_execution import RecommendationExecution


@pytest.fixture
def context():
    return {
        "request_id": str(uuid4()),
        "trip_id": str(uuid4()),
        "condition_version": 0,
        "current_conditions": {
            "origin": {
                "label": "서울",
                "coordinates": {"latitude": 37.56, "longitude": 126.97},
                "address": None,
                "region": "seoul",
                "supported": True,
            },
            "purposes": [{"purpose": "cafe", "keyword": None}],
            "group_type": None,
            "age_groups": None,
            "headcount": None,
            "max_travel_seconds": None,
        },
        "excluded_place_ids": [],
    }


def candidate(identifier, latitude=37.57, category="CE7"):
    return Candidate(
        place=Place(
            place_id="kakao:" + identifier,
            name="장소 " + identifier,
            purpose="cafe",
            category_code=category,
            category_name=None,
            coordinates={"latitude": latitude, "longitude": 126.98},
            address=None,
            road_address=None,
            phone=None,
            provider_url=None,
        ),
        collected_at=datetime.now(UTC),
    )


@pytest.fixture
def ports(app, geography):
    source, model, kakao = AsyncMock(), AsyncMock(), AsyncMock()
    source.region.return_value = "seoul"
    source.search.return_value = ([candidate("b"), candidate("a")], True)
    source.route.return_value = (600, 1000)
    source.parking.return_value = unavailable_parking()

    async def complete(instructions, payload, output_type):
        assert output_type is ExplanationPlan  # R01 must not interpret conditions.
        return {
            "items": [
                {"place_id": item["place_id"], "facts": ["eta"]}
                for item in payload["ordered_candidates"]
            ]
        }

    model.complete.side_effect = complete
    app.dependency_overrides[get_recommendation_source] = lambda: source
    app.dependency_overrides[get_ai_provider] = lambda: model
    app.dependency_overrides[get_kakao_local_client] = lambda: kakao
    execution = RecommendationExecution(MemoryProgressStore())
    app.dependency_overrides[get_recommendation_execution] = lambda: execution
    return source, model, kakao


def test_structured_request_uses_engine_and_existing_envelope(client, context, ports):
    response = client.post("/api/v1/recommendations", json=context | {"conditions_patch": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == context["request_id"] and body["error"] is None
    data = body["data"]
    assert data["kind"] == "recommendation"
    assert data["stages"] == {"interpretation": "skipped", "routes": "ok", "explanation": "ok"}
    assert [c["place"]["value"]["place_id"] for c in data["result"]["cards"]] == [
        "kakao:a",
        "kakao:b",
    ]
    assert data["condition_update"]["version"] == 0
    assert response.headers["cache-control"] == "no-store"
    ports[1].complete.assert_awaited_once()


def test_excluded_wrong_category_and_nonseoul_are_not_routed(client, context, ports):
    source, _, _ = ports
    context["excluded_place_ids"] = ["kakao:excluded"]
    source.search.return_value = (
        [
            candidate("excluded"),
            candidate("wrong", category="FD6"),
            candidate("outside", latitude=35.0),
            candidate("valid"),
        ],
        True,
    )
    source.region.side_effect = lambda coordinates: (
        "other" if coordinates.latitude == 35 else "seoul"
    )
    response = client.post("/api/v1/recommendations", json=context | {"conditions_patch": {}})
    assert response.status_code == 200
    assert [
        c["place"]["value"]["place_id"] for c in response.json()["data"]["result"]["cards"]
    ] == ["kakao:valid"]
    source.route.assert_awaited_once()


@pytest.mark.parametrize("all_failed", [False, True])
def test_route_failures_keep_general_candidates_separate(client, context, ports, all_failed):
    source, _, _ = ports
    source.route.side_effect = [
        ServiceError("PROVIDER_TIMEOUT", "조회 실패"),
        ServiceError("PROVIDER_ERROR", "조회 실패") if all_failed else (600, 1000),
    ]
    response = client.post("/api/v1/recommendations", json=context | {"conditions_patch": {}})
    data = response.json()["data"]
    assert response.status_code == 200
    assert data["kind"] == ("general_candidates" if all_failed else "recommendation")
    assert data["stages"]["routes"] == ("failed" if all_failed else "partial")
    if all_failed:
        assert all(
            c["rank"] is None and c["route"]["value"] is None for c in data["result"]["cards"]
        )
        source.parking.assert_not_awaited()


def test_time_limit_filters_seconds_without_rounding(client, context, ports):
    ports[0].route.side_effect = [(600, 1000), (601, 1000)]
    response = client.post(
        "/api/v1/recommendations", json=context | {"conditions_patch": {"max_travel_seconds": 600}}
    )
    data = response.json()["data"]
    assert response.status_code == 200
    assert [c["place"]["value"]["place_id"] for c in data["result"]["cards"]] == ["kakao:b"]
    assert data["condition_update"]["changes"] == [
        {"field": "max_travel_seconds", "before": None, "after": 600}
    ]


@pytest.mark.parametrize("mode", ["quota", "explanation", "parking", "origin"])
def test_errors_and_fallbacks_follow_shared_contract(client, context, ports, mode):
    source, model, _ = ports
    if mode == "quota":
        source.search.side_effect = ServiceError("RATE_LIMITED", "호출 제한", retry_after_seconds=5)
    elif mode == "explanation":
        model.complete.side_effect = ServiceError("PROVIDER_TIMEOUT", "모델 지연")
    elif mode == "parking":
        source.parking.side_effect = ServiceError("DATA_UNAVAILABLE", "DB 지연")
    else:
        source.region.return_value = "other"
    response = client.post("/api/v1/recommendations", json=context | {"conditions_patch": {}})
    body = response.json()
    assert response.status_code == {"quota": 429, "origin": 422}.get(mode, 200)
    if mode == "quota":
        assert body["error"]["code"] == "RATE_LIMITED"
        assert body["data"]["condition_update"]["applied"] is True
        assert response.headers["retry-after"] == "5"
    elif mode == "origin":
        assert body["error"]["code"] == "ORIGIN_OUT_OF_SERVICE_AREA"
        assert body["data"]["condition_update"]["applied"] is False
        source.search.assert_not_awaited()
    elif mode == "explanation":
        assert body["data"]["stages"]["explanation"] == "fallback"
        assert all(c["reason"]["source"] == "fallback" for c in body["data"]["result"]["cards"])
        assert "EXPLANATION_FALLBACK" in [w["code"] for w in body["data"]["warnings"]]
    else:
        assert all(c["parking"]["status"] == "error" for c in body["data"]["result"]["cards"])


@pytest.mark.parametrize("mode", ["reorder", "unknown_parking", "missing_eta"])
def test_ungrounded_explanation_cannot_change_rank_or_facts(client, context, ports, mode):
    async def complete(instructions, payload, output_type):
        items = [
            {"place_id": candidate["place_id"], "facts": ["eta"]}
            for candidate in payload["ordered_candidates"]
        ]
        if mode == "reorder":
            items.reverse()
        elif mode == "unknown_parking":
            items[0]["facts"] = ["eta", "parking"]
        else:
            items[0]["facts"] = ["parking"]
        return {"items": items}

    ports[1].complete.side_effect = complete
    response = client.post("/api/v1/recommendations", json=context | {"conditions_patch": {}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stages"]["explanation"] == "fallback"
    assert [c["place"]["value"]["place_id"] for c in data["result"]["cards"]] == [
        "kakao:a",
        "kakao:b",
    ]
    assert all("주차" not in c["reason"]["text"] for c in data["result"]["cards"])


def test_chat_calls_same_recommendation_engine_after_interpretation(client, context, ports):
    source, model, _ = ports
    original = model.complete.side_effect

    async def complete(instructions, payload, output_type):
        if output_type is Interpretation:
            assert "excluded_place_ids" not in payload
            return Interpretation(intent="recommendation", headcount=2)
        return await original(instructions, payload, output_type)

    model.complete.side_effect = complete
    response = client.post("/api/v1/chat/messages", json=context | {"message": "두 명이야"})
    data = response.json()["data"]
    assert response.status_code == 200
    assert data["kind"] == "recommendation" and data["stages"]["interpretation"] == "ok"
    assert data["condition_update"]["conditions"]["headcount"] == 2
    assert data["condition_update"]["changes"] == [
        {"field": "headcount", "before": None, "after": 2}
    ]
    source.search.assert_awaited_once()


def test_chat_clarification_keeps_pending_changes_unapplied(client, context, ports):
    source, model, _ = ports
    context["current_conditions"]["origin"] = None
    model.complete.side_effect = None
    model.complete.return_value = Interpretation(intent="recommendation", headcount=2)
    response = client.post("/api/v1/chat/messages", json=context | {"message": "두 명"})
    data = response.json()["data"]
    assert response.status_code == 200 and data["kind"] == "clarification"
    assert data["clarification"]["required_fields"] == ["origin"]
    assert data["clarification"]["pending_patch"]["headcount"] == 2
    assert data["condition_update"]["applied"] is False
    source.search.assert_not_awaited()


def test_chat_confirms_exact_origin_then_revalidates_selected_coordinates(client, context, ports):
    source, model, kakao = ports
    original = model.complete.side_effect

    async def complete(instructions, payload, output_type):
        if output_type is Interpretation:
            return Interpretation(intent="recommendation", origin_name="시청역")
        return await original(instructions, payload, output_type)

    model.complete.side_effect = complete
    kakao.keyword.return_value = {
        "documents": [
            {
                "id": "1",
                "place_name": "시청역",
                "x": "126.977",
                "y": "37.5657",
                "address_name": "서울 중구",
                "category_group_code": "SW8",
            }
        ],
        "meta": {"is_end": True},
    }
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
    response = client.post("/api/v1/chat/messages", json=context | {"message": "시청역 출발"})
    assert response.status_code == 200
    first = response.json()["data"]
    assert first["kind"] == "clarification" and not first["condition_update"]["applied"]
    question = first["clarification"]
    assert question["origin_context"]["mode"] == "confirm"
    source.search.assert_not_awaited()
    source.region.assert_not_awaited()
    selected = client.post(
        "/api/v1/chat/messages",
        json=context
        | {
            "request_id": str(uuid4()),
            "message": "여기서 출발",
            "origin_action": {
                "action": "select",
                "context_token": question["origin_context"]["context_token"],
                "candidate_id": question["choices"][0]["value"]["candidate_id"],
            },
        },
    )
    assert selected.status_code == 200
    assert selected.json()["data"]["kind"] == "recommendation"
    assert selected.json()["data"]["stages"]["interpretation"] == "skipped"
    assert source.region.call_args_list[0].args[0].latitude == 37.5657


def test_chat_interpretation_failure_preserves_conditions(client, context, ports):
    ports[1].complete.side_effect = ServiceError("AI_INTERPRETATION_FAILED", "형식 오류")
    response = client.post("/api/v1/chat/messages", json=context | {"message": "질문"})
    data = response.json()["data"]
    assert response.status_code == 502 and data["stages"]["interpretation"] == "failed"
    assert data["condition_update"]["conditions"] == context["current_conditions"]
    ports[0].search.assert_not_awaited()


def test_obsolete_natural_request_is_not_a_second_recommendations_contract(client, ports):
    response = client.post("/api/v1/recommendations", json={"message": "카페", "conditions": {}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    ports[0].search.assert_not_awaited()
    ports[1].complete.assert_not_awaited()


def test_nested_request_conditions_are_not_mutated(client, context, ports):
    before = deepcopy(context)
    client.post("/api/v1/recommendations", json=context | {"conditions_patch": {"headcount": 2}})
    assert context == before
    Conditions.model_validate(context["current_conditions"])
