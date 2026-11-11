"""Real app lifespan with a delayed, in-memory Bedrock transport."""

import time

from fastapi import Request
from pydantic import BaseModel

from app.main import create_app
from app.modules.ai.providers.bedrock import BedrockProvider
from jobs.runtime import report


class Answer(BaseModel):
    value: str


class Client:
    def converse(self, **kwargs):
        report({"status": "model_thread_started"})
        time.sleep(4)
        report({"status": "model_thread_finished"})
        return {
            "stopReason": "tool_use",
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "name": "Answer",
                                "input": {"value": "ok"},
                                "toolUseId": "test",
                            }
                        }
                    ],
                }
            },
        }

    def close(self):
        report({"status": "model_client_closed"})


def create():
    def client(provider):
        if provider._client is None:
            provider._client = Client()
        return provider._client

    BedrockProvider._get_client = client
    app = create_app()

    @app.get("/test-shutdown-ai")
    async def delayed(request: Request):
        result = await request.app.state.ai_provider.complete("test", {}, Answer)
        return result.model_dump()

    return app
