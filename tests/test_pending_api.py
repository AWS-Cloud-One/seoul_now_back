"""Transport/adapter tests; no real AI, collection, token store or DB writes."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.contract import ContractError
from app.core.config import Settings
from app.main import create_app
from app.schemas.pending import ChatData
from app.services.pending import (
    PendingFailure,
    PendingService,
    get_pending_service,
    require_data_status_access,
)


@pytest.fixture
def api():
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def context():
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
        "excluded_place_ids": ["previous"],
    }


def answer_data(context):
    return {
        "trip_id": context["trip_id"],
        "base_condition_version": 0,
        "kind": "answer",
        "message": "지원 범위 안내",
        "condition_update": {
            "applied": False,
            "conditions": context["current_conditions"],
            "version": 0,
            "changes": [],
        },
        "stages": {"interpretation": "ok", "routes": "skipped", "explanation": "skipped"},
        "result": None,
        "clarification": None,
        "warnings": [],
        "answer": {
            "topic": "scope_notice",
            "place_id": None,
            "text": "지원 범위 안내",
            "original_result_id": None,
            "original_recommended_at": None,
            "checked_at": None,
            "information": [],
        },
    }


@pytest.mark.parametrize("retry", [False, True])
def test_unconnected_conversation_preserves_context(api, context, retry):
    _, client = api
    body = context | (
        {"result_id": "r1", "context_token": "opaque"} if retry else {"message": "카페 찾아줘"}
    )
    url = "/recommendations/retry-routes" if retry else "/chat/messages"
    response = client.post("/api/v1" + url, json=body)
    assert response.status_code == 503
    result = response.json()
    assert result["request_id"] == context["request_id"]
    assert result["data"]["condition_update"]["conditions"] == context["current_conditions"]
    assert result["data"]["condition_update"]["applied"] is False
    assert result["data"]["kind"] == "failure"
    assert result["error"]["retryable"] is False
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "patch",
    [
        {"message": ""},
        {"message": "x" * 501},
        {"recent_messages": [{"role": "system", "content": "x"}]},
        {"recent_messages": [{"role": "user", "content": "x"}] * 11},
        {"recent_messages": [{"role": "user", "content": "x" * 501}]},
        {"condition_version": True},
        {"target": {"place_id": "x"}},
    ],
)
def test_chat_validation(api, context, patch):
    response = api[1].post("/api/v1/chat/messages", json=context | {"message": "질문"} | patch)
    assert response.status_code == 422
    assert response.json()["request_id"] == context["request_id"]
    assert response.json()["data"] is None


def test_chat_adapter_receives_context_and_answer_once(api, context):
    app, client = api
    adapter = AsyncMock()
    adapter.process.return_value = answer_data(context)
    app.dependency_overrides[get_pending_service] = lambda: PendingService(chat=adapter)
    response = client.post("/api/v1/chat/messages", json=context | {"message": "지원 지역?"})
    assert response.status_code == 200
    assert response.json()["data"]["answer"]["topic"] == "scope_notice"
    adapter.process.assert_awaited_once()
    assert adapter.process.call_args.args[0].excluded_place_ids == ["previous"]


def test_adapter_failure_retains_accepted_conditions(api, context):
    app, client = api
    data = answer_data(context)
    data.update(kind="failure", answer=None)
    data["condition_update"].update(applied=True, version=1)
    data["condition_update"]["conditions"] = deepcopy(context["current_conditions"])
    data["condition_update"]["conditions"]["headcount"] = 2
    data["condition_update"]["changes"] = [{"field": "headcount", "before": None, "after": 2}]
    adapter = AsyncMock()
    adapter.process.side_effect = PendingFailure(
        429,
        "RATE_LIMITED",
        "호출 한도 초과",
        data=ChatData.model_validate(data),
        retry_after_seconds=5,
    )
    app.dependency_overrides[get_pending_service] = lambda: PendingService(chat=adapter)
    response = client.post("/api/v1/chat/messages", json=context | {"message": "두 명"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert response.json()["data"]["condition_update"]["conditions"]["headcount"] == 2


@pytest.mark.parametrize("bad_context", [False, True])
def test_adapter_invalid_context_and_exceptions_are_safe(api, context, bad_context):
    app, client = api
    adapter = AsyncMock()
    if bad_context:
        data = answer_data(context)
        data["trip_id"] = str(uuid4())
        adapter.process.return_value = data
    else:
        adapter.process.side_effect = RuntimeError("secret-provider-url")
    app.dependency_overrides[get_pending_service] = lambda: PendingService(chat=adapter)
    response = client.post("/api/v1/chat/messages", json=context | {"message": "질문"})
    assert response.status_code == 500
    assert "secret-provider-url" not in response.text
    assert response.json()["data"]["condition_update"]["applied"] is False


def test_place_requires_context_and_does_not_fake_success(api):
    client = api[1]
    assert client.get("/api/v1/places/p1").status_code == 422
    response = client.get("/api/v1/places/p1", headers={"Result-Context": "opaque"})
    assert response.status_code == 503
    assert response.json()["data"] is None


def test_travel_request_and_context_failure(api, context):
    app, client = api
    body = {key: context[key] for key in ("request_id", "trip_id", "condition_version")}
    body.update(result_id="r1", context_token="opaque")
    response = client.post("/api/v1/places/p1/travel-time", json=body)
    assert response.status_code == 503
    assert response.json()["request_id"] == body["request_id"]
    adapter = AsyncMock()
    adapter.check.side_effect = ContractError(410, "CONTEXT_EXPIRED", "문맥 만료")
    app.dependency_overrides[get_pending_service] = lambda: PendingService(travel=adapter)
    response = client.post("/api/v1/places/p1/travel-time", json=body)
    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"


def test_admin_default_blocks_reader_and_explicit_guard_allows_sanitized_data(api):
    app, client = api
    reader = AsyncMock()
    reader.read.return_value = {
        "source": "test",
        "target_key": "all",
        "last_run": None,
        "last_completed_run": None,
        "last_received_at": None,
    }
    app.dependency_overrides[get_pending_service] = lambda: PendingService(status=reader)
    url = "/api/v1/data-status?source=test&target_key=all"
    assert client.get(url).status_code == 503
    reader.read.assert_not_awaited()
    app.dependency_overrides[require_data_status_access] = lambda: None
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["data"]["last_run"] is None
    reader.read.return_value["raw_payload"] = "private"
    response = client.get(url)
    assert response.status_code == 500
    assert "private" not in response.text


def test_travel_adapter_returns_matching_context(api, context):
    app, client = api
    adapter = AsyncMock()
    now = datetime.now(UTC).isoformat()
    adapter.check.return_value = {
        "trip_id": context["trip_id"],
        "base_condition_version": 0,
        "result_id": "r1",
        "place_id": "p1",
        "original_recommended_at": None,
        "checked_at": now,
        "route": {
            "value": {"duration_seconds": 600, "distance_meters": 1000},
            "status": "available",
            "freshness": "fresh",
            "observed_at": None,
            "collected_at": now,
            "source": "kakao_mobility",
            "reason_code": None,
        },
    }
    app.dependency_overrides[get_pending_service] = lambda: PendingService(travel=adapter)
    body = {key: context[key] for key in ("request_id", "trip_id", "condition_version")}
    body.update(result_id="r1", context_token="opaque")
    response = client.post("/api/v1/places/p1/travel-time", json=body)
    assert response.status_code == 200
    assert response.json()["data"]["route"]["value"]["duration_seconds"] == 600
    adapter.check.return_value["place_id"] = "another-place"
    assert client.post("/api/v1/places/p1/travel-time", json=body).status_code == 500


def test_openapi_and_context_header_cors(api):
    client = api[1]
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/chat/messages",
        "/recommendations/retry-routes",
        "/places/{place_id}",
        "/places/{place_id}/travel-time",
        "/data-status",
    ):
        assert "/api/v1" + path in paths
    response = client.options(
        "/api/v1/places/p1",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Result-Context",
        },
    )
    assert response.status_code == 200


def test_malformed_chat_json(api):
    response = api[1].post(
        "/api/v1/chat/messages", content="{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mismatch", [False, True])
def test_retry_adapter_forwards_original_context_without_search(api, context, mismatch):
    app, client = api
    adapter = AsyncMock()
    data = answer_data(context)
    data.update(kind="no_candidates", answer=None)
    data["stages"]["interpretation"] = "skipped"
    if mismatch:
        data["condition_update"]["conditions"] = deepcopy(context["current_conditions"])
        data["condition_update"]["conditions"]["headcount"] = 2
        data["condition_update"].update(applied=True, version=1)
    adapter.retry.return_value = data
    app.dependency_overrides[get_pending_service] = lambda: PendingService(retry=adapter)
    response = client.post(
        "/api/v1/recommendations/retry-routes",
        json=context | {"result_id": "r1", "context_token": "opaque"},
    )
    assert response.status_code == (500 if mismatch else 200)
    adapter.retry.assert_awaited_once()
    request = adapter.retry.call_args.args[0]
    assert request.result_id == "r1" and request.context_token == "opaque"
    assert request.excluded_place_ids == ["previous"]


def test_place_adapter_receives_header_and_rejects_another_place(api):
    app, client = api
    adapter = AsyncMock()
    now = datetime.now(UTC).isoformat()
    adapter.detail.return_value = {
        "result_id": "r1",
        "original_recommended_at": None,
        "checked_at": now,
        "place": {
            "value": {
                "place_id": "p1",
                "name": "테스트 장소",
                "purpose": None,
                "category_code": None,
                "category_name": None,
                "coordinates": {"latitude": 37.5665, "longitude": 126.978},
                "address": None,
                "road_address": None,
                "phone": None,
                "provider_url": None,
            },
            "status": "available",
            "freshness": "unknown",
            "observed_at": None,
            "collected_at": now,
            "source": "kakao_local",
            "reason_code": None,
        },
        "parking": {
            "status": "unavailable",
            "reason_code": "NO_RECORD",
            "checked_at": now,
            "search_radius_meters": None,
            "nearest_distance_meters": None,
            "items": [],
            "total_count": None,
            "next_cursor": None,
        },
    }
    app.dependency_overrides[get_pending_service] = lambda: PendingService(places=adapter)
    response = client.get("/api/v1/places/p1", headers={"Result-Context": "opaque"})
    assert response.status_code == 200
    adapter.detail.assert_awaited_once_with("p1", "opaque")
    adapter.detail.return_value["place"]["value"]["place_id"] = "other"
    response = client.get("/api/v1/places/p1", headers={"Result-Context": "opaque"})
    assert response.status_code == 500
