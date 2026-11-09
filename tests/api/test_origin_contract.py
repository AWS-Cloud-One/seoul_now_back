"""Origin selection wire validation and missing-context rejection."""

import pytest

from tests.api.test_recommendation_integration import context as context
from tests.api.test_recommendation_integration import ports as ports
from tests.fixtures.origin import ORIGIN_TOKEN, origin_clarification


@pytest.mark.parametrize(
    "action",
    [
        {"action": "select", "context_token": ORIGIN_TOKEN},
        {"action": "select", "context_token": "", "candidate_id": "origin-1"},
        {"action": "restart", "context_token": ORIGIN_TOKEN, "candidate_id": "origin-1"},
        {
            "action": "select",
            "context_token": ORIGIN_TOKEN,
            "candidate_id": "origin-1",
            "location": {"label": "변조한 위치"},
        },
    ],
)
def test_invalid_origin_action_is_rejected_before_model_or_provider(client, context, ports, action):
    source, model, kakao = ports
    result = client.post(
        "/api/v1/chat/messages", json=context | {"message": "선택", "origin_action": action}
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not model.mock_calls and not source.mock_calls and not kakao.mock_calls


@pytest.mark.parametrize("kind", ["select", "show_alternatives", "restart", "pending_question"])
def test_unknown_origin_context_never_falls_through_to_recommendation(client, context, ports, kind):
    source, model, kakao = ports
    body = context | {"message": "여기서 출발"}
    if kind == "pending_question":
        body["pending_clarification"] = origin_clarification()
    else:
        body["origin_action"] = {"action": kind, "context_token": ORIGIN_TOKEN}
        if kind == "select":
            body["origin_action"]["candidate_id"] = "origin-1"
    result = client.post("/api/v1/chat/messages", json=body)
    assert result.status_code == 410
    wire = result.json()
    assert wire["error"]["code"] == "CONTEXT_EXPIRED"
    assert wire["error"]["retryable"] is False
    assert wire["data"]["condition_update"]["applied"] is False
    assert wire["data"]["condition_update"]["conditions"] == context["current_conditions"]
    assert wire["data"]["stages"]["interpretation"] == "skipped"
    assert not model.mock_calls and not source.mock_calls and not kakao.mock_calls


def test_openapi_documents_optional_origin_contract_without_requiring_it(app):
    models = app.openapi()["components"]["schemas"]
    request = models["ChatRequest"]
    assert "origin_action" in request["properties"]
    assert "origin_action" not in request["required"]
    assert "OriginAction" in models and "OriginClarificationContext" in models
    assert models["OriginAction"]["properties"]["action"]["enum"] == [
        "select",
        "show_alternatives",
        "restart",
    ]
