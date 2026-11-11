"""Bedrock Converse transport with validated tool output, without tool execution."""

import asyncio
import json
from threading import Lock

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
    TokenRetrievalError,
)
from pydantic import BaseModel

from app.core.config import Settings
from app.core.exceptions import ServiceError


class BedrockProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._client_lock = Lock()

    def _get_client(self):
        # Resolve/refresh credentials on a worker thread, never on the ASGI event loop.
        # Lazy creation keeps health checks and non-AI routes independent of AWS access.
        with self._client_lock:
            if self._client is None:
                self._client = boto3.Session().client(
                    "bedrock-runtime",
                    region_name=self.settings.bedrock_region,
                    config=Config(
                        connect_timeout=5,
                        read_timeout=self.settings.bedrock_timeout_seconds,
                        retries={"total_max_attempts": 2, "mode": "adaptive"},
                    ),
                )
            return self._client

    async def aclose(self):
        await asyncio.to_thread(self._close)

    def _close(self):
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    async def complete[T: BaseModel](
        self, instructions: str, payload: dict, output_type: type[T]
    ) -> T:
        return await asyncio.to_thread(self._complete, instructions, payload, output_type)

    def _complete[T: BaseModel](self, instructions: str, payload: dict, output_type: type[T]) -> T:
        try:
            client = self._get_client()
            try:
                response = client.converse(
                    modelId=self.settings.bedrock_model_id,
                    system=[{"text": instructions}],
                    messages=[
                        {
                            "role": "user",
                            "content": [{"text": json.dumps(payload, ensure_ascii=False)}],
                        }
                    ],
                    inferenceConfig={"maxTokens": self.settings.bedrock_max_tokens},
                    toolConfig={
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
                )
            except client.exceptions.ThrottlingException:
                raise ServiceError("RATE_LIMITED", "모델 호출 한도를 초과했습니다.") from None
            except client.exceptions.ModelTimeoutException:
                raise ServiceError("PROVIDER_TIMEOUT", "모델 응답 시간이 초과되었습니다.") from None
            except (
                client.exceptions.AccessDeniedException,
                client.exceptions.ValidationException,
                client.exceptions.ResourceNotFoundException,
                client.exceptions.ServiceQuotaExceededException,
            ):
                raise ServiceError(
                    "PROVIDER_ERROR",
                    "모델 제공처 요청 설정 또는 권한을 확인해야 합니다.",
                    retryable=False,
                ) from None
        except ConnectTimeoutError, ReadTimeoutError:
            raise ServiceError("PROVIDER_TIMEOUT", "모델 응답 시간이 초과되었습니다.") from None
        except (
            NoCredentialsError,
            PartialCredentialsError,
            CredentialRetrievalError,
            TokenRetrievalError,
        ):
            raise ServiceError(
                "SERVICE_UNAVAILABLE", "모델 제공처 인증이 준비되지 않았습니다.", retryable=False
            ) from None
        except ClientError, BotoCoreError:
            # This transport boundary must not expose AWS error bodies or credentials.
            raise ServiceError("PROVIDER_ERROR", "모델 제공처 요청에 실패했습니다.") from None

        try:
            if response["stopReason"] != "tool_use":
                raise ValueError("incomplete or rejected output")
            message = response["output"]["message"]
            if message["role"] != "assistant":
                raise ValueError("unexpected role")
            tools = [part["toolUse"] for part in message["content"] if "toolUse" in part]
            if len(tools) != 1 or tools[0]["name"] != output_type.__name__:
                raise ValueError("unexpected tool output")
            return output_type.model_validate(tools[0]["input"])
        except ValueError, TypeError, KeyError, AttributeError:
            raise ServiceError(
                "AI_INTERPRETATION_FAILED", "모델의 구조화 응답을 검증할 수 없습니다."
            ) from None
