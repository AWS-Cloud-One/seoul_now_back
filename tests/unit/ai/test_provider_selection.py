import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.main import create_app
from app.modules.ai.interpretation import Interpretation
from app.modules.ai.providers.bedrock import BedrockProvider
from app.modules.ai.providers.factory import open_ai_provider
from app.modules.ai.providers.openai import OpenAIProvider


def test_default_provider_starts_without_aws_or_openai_access(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Health and provider selection must not authenticate or call a model")

    monkeypatch.setattr("app.modules.ai.providers.bedrock.boto3.Session", unexpected)
    monkeypatch.setattr("app.modules.ai.providers.factory.OpenAIProvider", unexpected)
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        assert isinstance(app.state.ai_provider, BedrockProvider)
        assert client.get("/api/v1/health/live").status_code == 200


def test_openai_remains_explicitly_selectable_and_closes_http_client(monkeypatch):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "content": [
                                {"type": "output_text", "text": '{"intent":"recommendation"}'}
                            ]
                        }
                    ],
                },
            )
        )
    )
    monkeypatch.setattr("app.modules.ai.providers.factory.httpx.AsyncClient", lambda: http)
    monkeypatch.setattr(
        "app.modules.ai.providers.factory.BedrockProvider",
        Mock(side_effect=AssertionError("Do not initialize Bedrock")),
    )

    async def run():
        async with open_ai_provider(
            Settings(_env_file=None, ai_provider="openai", openai_api_key="test-key")
        ) as provider:
            assert isinstance(provider, OpenAIProvider)
            value = await provider.complete("instructions", {}, Interpretation)
            assert value.intent == "recommendation"

    asyncio.run(run())
    assert http.is_closed


def test_bedrock_failure_does_not_silently_call_openai(monkeypatch):
    monkeypatch.setattr(
        "app.modules.ai.providers.factory.OpenAIProvider",
        Mock(side_effect=AssertionError("No implicit fallback")),
    )

    async def run():
        async with open_ai_provider(
            Settings(_env_file=None, openai_api_key="test-key")
        ) as provider:
            monkeypatch.setattr(
                provider,
                "_get_client",
                Mock(side_effect=ServiceError("PROVIDER_ERROR", "Bedrock unavailable")),
            )
            with pytest.raises(ServiceError, match="Bedrock unavailable"):
                await provider.complete("instructions", {}, Interpretation)

    asyncio.run(run())
