import asyncio
import json
import threading
from contextlib import closing
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.modules.ai.explanation import ExplanationPlan
from app.modules.ai.interpretation import Interpretation
from app.modules.ai.providers.bedrock import BedrockProvider


@pytest.fixture
def client():
    with closing(
        boto3.Session().client(
            "bedrock-runtime",
            region_name="ap-northeast-2",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
    ) as client:
        yield client


def response(value=None, *, name="Interpretation", stop="tool_use"):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "response-1",
                            "name": name,
                            "input": value if value is not None else {"intent": "recommendation"},
                        }
                    }
                ],
            }
        },
        "stopReason": stop,
        "usage": {"inputTokens": 10, "outputTokens": 10, "totalTokens": 20},
        "metrics": {"latencyMs": 10},
    }


def provider_with(client, monkeypatch):
    provider = BedrockProvider(Settings(_env_file=None))
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider


@pytest.mark.parametrize(
    "output_type,value",
    [
        (Interpretation, {"intent": "recommendation", "origin_name": "강남역"}),
        (ExplanationPlan, {"items": [{"place_id": "place-1", "facts": ["eta", "parking"]}]}),
    ],
)
def test_converse_uses_full_contract_and_validates_output(client, monkeypatch, output_type, value):
    provider = provider_with(client, monkeypatch)
    payload = {"message": "강남역에서 출발할게"}
    expected = {
        "modelId": provider.settings.bedrock_model_id,
        "system": [{"text": "instructions"}],
        "messages": [
            {"role": "user", "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}
        ],
        "inferenceConfig": {"maxTokens": 2000},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": output_type.__name__,
                        "description": "Return the requested structured response.",
                        "inputSchema": {"json": output_type.model_json_schema()},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": output_type.__name__}},
        },
    }
    with Stubber(client) as stub:
        stub.add_response("converse", response(value, name=output_type.__name__), expected)
        result = asyncio.run(provider.complete("instructions", payload, output_type))
        assert result == output_type.model_validate(value)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "stop",
    ["max_tokens", "guardrail_intervened", "content_filtered", "end_turn", "malformed_tool_use"],
)
def test_incomplete_or_blocked_output_is_never_accepted(client, monkeypatch, stop):
    provider = provider_with(client, monkeypatch)
    with Stubber(client) as stub:
        stub.add_response("converse", response(stop=stop))
        with pytest.raises(ServiceError) as error:
            asyncio.run(provider.complete("instructions", {}, Interpretation))
        assert error.value.code == "AI_INTERPRETATION_FAILED"


@pytest.mark.parametrize(
    "value,name",
    [
        ({"intent": "invalid"}, "Interpretation"),
        ({"intent": "recommendation", "headcount": 0}, "Interpretation"),
        ({"intent": "recommendation", "unknown": "secret"}, "Interpretation"),
        ({"intent": "recommendation"}, "UnexpectedTool"),
    ],
)
def test_unknown_tool_and_invalid_contract_are_rejected(client, monkeypatch, value, name):
    provider = provider_with(client, monkeypatch)
    with Stubber(client) as stub:
        stub.add_response("converse", response(value, name=name))
        with pytest.raises(ServiceError) as error:
            asyncio.run(provider.complete("instructions", {}, Interpretation))
        assert error.value.code == "AI_INTERPRETATION_FAILED"
        assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        [{"text": '{"intent":"recommendation"}'}],
        response()["output"]["message"]["content"] * 2,
    ],
)
def test_missing_or_multiple_tool_calls_are_rejected(client, monkeypatch, content):
    provider = provider_with(client, monkeypatch)
    body = response()
    body["output"]["message"]["content"] = content
    with Stubber(client) as stub:
        stub.add_response("converse", body)
        with pytest.raises(ServiceError) as error:
            asyncio.run(provider.complete("instructions", {}, Interpretation))
        assert error.value.code == "AI_INTERPRETATION_FAILED"


@pytest.mark.parametrize(
    "aws_code,code,retryable",
    [
        ("ThrottlingException", "RATE_LIMITED", None),
        ("ModelTimeoutException", "PROVIDER_TIMEOUT", None),
        ("AccessDeniedException", "PROVIDER_ERROR", False),
        ("ValidationException", "PROVIDER_ERROR", False),
        ("ResourceNotFoundException", "PROVIDER_ERROR", False),
        ("ServiceQuotaExceededException", "PROVIDER_ERROR", False),
        ("ServiceUnavailableException", "PROVIDER_ERROR", None),
        ("InternalServerException", "PROVIDER_ERROR", None),
    ],
)
def test_aws_errors_use_safe_application_errors(client, monkeypatch, aws_code, code, retryable):
    provider = provider_with(client, monkeypatch)
    with Stubber(client) as stub:
        stub.add_client_error("converse", aws_code, service_message="private AWS details")
        with pytest.raises(ServiceError) as error:
            asyncio.run(provider.complete("instructions", {}, Interpretation))
        assert error.value.code == code
        assert error.value.retryable is retryable
        assert "private AWS details" not in str(error.value)


@pytest.mark.parametrize(
    "failure,code",
    [
        (NoCredentialsError(), "SERVICE_UNAVAILABLE"),
        (ReadTimeoutError(endpoint_url="https://private-endpoint"), "PROVIDER_TIMEOUT"),
    ],
)
def test_credentials_and_transport_failures_are_safe(monkeypatch, failure, code):
    provider = BedrockProvider(Settings(_env_file=None))
    monkeypatch.setattr(provider, "_get_client", Mock(side_effect=failure))
    with pytest.raises(ServiceError) as error:
        asyncio.run(provider.complete("instructions", {}, Interpretation))
    assert error.value.code == code
    assert "private-endpoint" not in str(error.value)


def test_client_is_lazy_reused_off_event_loop_and_closed(monkeypatch):
    main_thread = threading.get_ident()
    client = Mock()

    def converse(**kwargs):
        assert threading.get_ident() != main_thread
        return response()

    client.converse.side_effect = converse
    session = Mock()

    def create_client(*args, **kwargs):
        assert threading.get_ident() != main_thread
        assert kwargs["config"].retries == {"total_max_attempts": 2, "mode": "adaptive"}
        return client

    session.client.side_effect = create_client
    monkeypatch.setattr("app.modules.ai.providers.bedrock.boto3.Session", lambda: session)
    provider = BedrockProvider(Settings(_env_file=None))
    session.client.assert_not_called()

    async def run():
        await asyncio.gather(
            *[provider.complete("instructions", {}, Interpretation) for _ in range(3)]
        )
        await provider.aclose()

    asyncio.run(run())
    session.client.assert_called_once()
    client.close.assert_called_once()
