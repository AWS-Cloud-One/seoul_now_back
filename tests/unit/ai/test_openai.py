import asyncio
import json

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.modules.ai.providers.openai import OpenAIProvider


def settings():
    return Settings(_env_file=None, openai_api_key="test-key")


def test_responses_schema_and_validated_output():
    async def run():
        def handler(request):
            data = json.loads(request.content)
            assert data["store"] is False
            schema = data["text"]["format"]["schema"]
            assert data["text"]["format"]["strict"] is True
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": Interpretation(
                                        intent="recommendation"
                                    ).model_dump_json(),
                                }
                            ],
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            value = await OpenAIProvider(http, settings()).complete(
                "instructions", {}, Interpretation
            )
            assert value.intent == "recommendation"

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body,code",
    [
        (429, {}, "RATE_LIMITED"),
        (401, {}, "PROVIDER_ERROR"),
        (500, {}, "PROVIDER_ERROR"),
        (200, {"status": "incomplete"}, "AI_INTERPRETATION_FAILED"),
        (
            200,
            {"status": "completed", "output": [{"content": [{"type": "refusal"}]}]},
            "AI_INTERPRETATION_FAILED",
        ),
        (
            200,
            {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": '{"intent":"bad"}'}]}],
            },
            "AI_INTERPRETATION_FAILED",
        ),
    ],
)
def test_openai_failure_mapping_without_raw_body_leaks(status, body, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body, headers={"Retry-After": "7"})
            )
        ) as http:
            with pytest.raises(ServiceError) as exc:
                await OpenAIProvider(http, settings()).complete("instructions", {}, Interpretation)
            assert exc.value.code == code
            assert "test-key" not in exc.value.message
            if status == 429:
                assert exc.value.retry_after_seconds == 7

    asyncio.run(run())


def test_missing_key_does_not_make_a_request():
    async def run():
        def handler(request):
            raise AssertionError("should not call a provider")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = OpenAIProvider(http, Settings(_env_file=None, openai_api_key=""))
            with pytest.raises(ServiceError) as exc:
                await provider.complete("instructions", {}, Interpretation)
            assert exc.value.code == "SERVICE_UNAVAILABLE"

    asyncio.run(run())
