from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.recommendations import Conditions, ConditionUpdate, RecommendationData, Stages
from app.services.recommendations import (
    RecommendationFailure,
    RecommendationService,
    ValidationDecision,
    get_recommendation_service,
)


@pytest.fixture
def client():
    app = create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://test:test@127.0.0.1:1/test")
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def payload():
    return {
        "request_id": str(uuid4()),
        "trip_id": str(uuid4()),
        "condition_version": 0,
        "current_conditions": {
            "origin": None,
            "purposes": [],
            "group_type": None,
            "age_groups": None,
            "headcount": None,
            "max_travel_seconds": None,
        },
        "conditions_patch": {
            "origin": {
                "label": "예시 출발지",
                "coordinates": {"latitude": 37.5547, "longitude": 126.9706},
                "address": None,
                "region": "seoul",
                "supported": True,
            },
            "purposes": [{"purpose": "cafe"}],
        },
        "excluded_place_ids": ["kakao:previous-place"],
    }


def test_missing_adapters_is_503_and_preserves_context(client, payload):
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 503
    body = response.json()
    assert body["request_id"] == payload["request_id"]
    assert body["data"]["trip_id"] == payload["trip_id"]
    assert body["data"]["kind"] == "failure"
    assert body["data"]["condition_update"]["applied"] is False
    assert body["data"]["condition_update"]["conditions"] == payload["current_conditions"]
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "field,value",
    [
        ("condition_version", True),
        ("condition_version", -1),
        ("unexpected", "field"),
        ("conditions_patch", {"origin": None}),
        ("conditions_patch", {"purposes": None}),
        ("conditions_patch", {"headcount": 0}),
    ],
)
def test_bad_wire_input(client, payload, field, value):
    payload[field] = value
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 422
    assert response.json()["request_id"] == payload["request_id"]
    assert response.json()["data"] is None


def test_malformed_json(client):
    response = client.post(
        "/api/v1/recommendations",
        content="{",
        headers={
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


def adapters(client, payload):
    # Test-only adapter output; production never uses a fake recommendation.
    validator, engine = AsyncMock(), AsyncMock()
    conditions = Conditions.model_validate(
        payload["current_conditions"] | payload["conditions_patch"]
    )
    update = ConditionUpdate(applied=True, conditions=conditions, version=1, changes=[])
    validator.validate.return_value = ValidationDecision(update)
    client.app.dependency_overrides[get_recommendation_service] = lambda: RecommendationService(
        validator,
        engine,
    )
    return validator, engine, update


def test_adapter_receives_exclusions_and_result_is_forwarded(client, payload):
    validator, engine, update = adapters(client, payload)
    engine.recommend.return_value = RecommendationData(
        trip_id=payload["trip_id"],
        base_condition_version=0,
        kind="no_candidates",
        message="현재 조건에서 후보가 없습니다.",
        condition_update=update,
        stages=Stages(routes="ok"),
        result=None,
        clarification=None,
        warnings=[],
    )
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["kind"] == "no_candidates"
    assert response.json()["data"]["stages"]["interpretation"] == "skipped"
    assert engine.recommend.await_args.args[0].excluded_place_ids == payload["excluded_place_ids"]
    validator.validate.assert_awaited_once()
    engine.recommend.assert_awaited_once()


def test_failure_after_acceptance_preserves_accepted_conditions(client, payload):
    _, engine, update = adapters(client, payload)
    engine.recommend.side_effect = RecommendationFailure(429, "RATE_LIMITED", "호출 제한", 30)
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.json()["data"]["condition_update"] == update.model_dump(mode="json")
    engine.recommend.assert_awaited_once()


def test_unexpected_failure_does_not_leak_provider_details(client, payload):
    _, engine, _ = adapters(client, payload)
    engine.recommend.side_effect = RuntimeError("secret-provider-key")
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 500
    assert "secret-provider-key" not in response.text


def test_explicit_null_and_omission_remain_distinct(client, payload):
    validator, _, _ = adapters(client, payload)
    payload["conditions_patch"] = {"max_travel_seconds": None}
    before = deepcopy(payload)
    client.post("/api/v1/recommendations", json=payload)
    request = validator.validate.await_args.args[0]
    assert request.conditions_patch.model_dump(exclude_unset=True) == {"max_travel_seconds": None}
    assert payload == before


def test_openapi_has_recommendation_contract(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/recommendations"]["post"]
    assert operation["tags"] == ["recommendations"]
    assert {"200", "400", "422", "503"} <= operation["responses"].keys()
