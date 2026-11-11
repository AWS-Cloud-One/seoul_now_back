"""Open exactly the configured provider; failures never switch providers implicitly."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.config import Settings
from app.modules.ai.providers.base import AIProvider
from app.modules.ai.providers.bedrock import BedrockProvider
from app.modules.ai.providers.openai import OpenAIProvider


@asynccontextmanager
async def open_ai_provider(settings: Settings) -> AsyncIterator[AIProvider]:
    if settings.ai_provider == "openai":
        async with httpx.AsyncClient() as client:
            yield OpenAIProvider(client, settings)
    else:
        provider = BedrockProvider(settings)
        try:
            yield provider
        finally:
            await provider.aclose()
