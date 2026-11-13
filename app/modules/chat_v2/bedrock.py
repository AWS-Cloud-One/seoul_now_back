"""Explicit V2 LangChain/Bedrock factory. Does not replace the V1 provider."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager, closing
from typing import Literal

import boto3
from botocore.config import Config
from langchain_aws import ChatBedrockConverse

from app.core.config import Settings
from app.modules.chat_v2.interpretation import InterpretationTrace, LangChainHeadcountInterpreter


@asynccontextmanager
async def open_bedrock_interpreter(
    settings: Settings,
    *,
    profile_name: str | None = None,
    observer: Callable[[InterpretationTrace], None] | None = None,
    mode: Literal["headcount", "dialogue"] = "headcount",
) -> AsyncIterator[LangChainHeadcountInterpreter]:
    """Resolve credentials lazily on a worker; use the normal role chain on AWS.

    Explicitly Bedrock-only. OpenAI remains the existing separately selected V1
    candidate; failures never trigger a second provider.
    """
    if settings.ai_provider != "bedrock":
        raise ValueError("V2 interpretation currently requires ai_provider=bedrock")
    if mode not in {"headcount", "dialogue"}:
        raise ValueError("Unknown interpretation mode")
    resources = ExitStack()

    def create_model():
        session = boto3.Session(profile_name=profile_name, region_name=settings.bedrock_region)
        config = Config(
            connect_timeout=5,
            read_timeout=settings.bedrock_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "adaptive"},
            max_pool_connections=2,
        )
        runtime = resources.enter_context(closing(session.client("bedrock-runtime", config=config)))
        control = resources.enter_context(closing(session.client("bedrock", config=config)))
        return ChatBedrockConverse(
            model_id=settings.bedrock_model_id,
            region_name=settings.bedrock_region,
            client=runtime,
            bedrock_client=control,
            max_tokens=min(settings.bedrock_max_tokens, 1024 if mode == "dialogue" else 512),
            temperature=0,
            disable_streaming=True,
            cache=False,
        )

    from app.modules.chat_v2.dialogue_interpretation import LangChainDialogueInterpreter

    interpreter_type = (
        LangChainDialogueInterpreter if mode == "dialogue" else LangChainHeadcountInterpreter
    )
    interpreter = interpreter_type(
        create_model,
        model_id=settings.bedrock_model_id,
        timeout_seconds=settings.bedrock_timeout_seconds,
        observer=observer,
    )
    try:
        yield interpreter
    finally:
        await interpreter.aclose()
        await asyncio.to_thread(resources.close)
