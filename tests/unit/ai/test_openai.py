import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from app.modules.ai.providers.openai import OpenAIProvider
from app.modules.recommendation.errors import ProviderError
from app.modules.recommendation.settings import RecommendationSettings as Settings
from app.schemas.recommendation import Interpretation


def run_response(payload, status=200):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            settings = Settings(_env_file=None, openai_api_key=SecretStr("test-only"))
            return await OpenAIProvider(client, settings).complete(
                "extract", {"message": "파스타"}, Interpretation
            )

    return run, requests


def test_responses_structured_output_and_no_conversation_storage():
    run, requests = run_response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "request_type": "new_recommendation",
                                    "changes": {"query": "파스타"},
                                }
                            ),
                        }
                    ],
                }
            ],
        }
    )
    assert asyncio.run(run()).changes.query == "파스타"
    assert requests[0]["store"] is False
    fmt = requests[0]["text"]["format"]
    assert fmt["strict"] is True
    assert fmt["schema"]["$defs"]["Changes"]["required"] == [
        "query",
        "origin_name",
        "max_minutes",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": [{"content": [{"type": "refusal"}]}]},
        {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}]},
    ],
)
def test_invalid_or_refused_model_output_is_not_empty_success(payload):
    run, _ = run_response(payload)
    with pytest.raises(ProviderError):
        asyncio.run(run())


def test_upstream_error_does_not_expose_credentials():
    run, _ = run_response({"error": "test-only"}, 401)
    with pytest.raises(ProviderError) as error:
        asyncio.run(run())
    assert "test-only" not in str(error.value)
