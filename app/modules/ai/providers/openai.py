"""Responses API transport, recovered from 3ce32df and using shared ServiceError."""

import json

import httpx

from app.core.exceptions import ServiceError


def strict_schema(schema):
    if isinstance(schema, dict):
        schema = {key: strict_schema(value) for key, value in schema.items() if key != "default"}
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
            schema["required"] = list(schema.get("properties", {}))
    elif isinstance(schema, list):
        schema = [strict_schema(value) for value in schema]
    return schema


class OpenAIProvider:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    async def complete(self, instructions, payload, output_type):
        key = self.settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ServiceError(
                "SERVICE_UNAVAILABLE",
                "자연어 처리 기능이 아직 설정되지 않았습니다.",
                retryable=False,
            )
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.settings.openai_model,
                    "store": False,
                    "instructions": instructions,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "max_output_tokens": 2000,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": output_type.__name__,
                            "strict": True,
                            "schema": strict_schema(output_type.model_json_schema()),
                        }
                    },
                },
                timeout=self.settings.openai_timeout_seconds,
            )
            if response.status_code == 429:
                raw = response.headers.get("Retry-After", "")
                wait = int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 9 else None
                raise ServiceError(
                    "RATE_LIMITED", "모델 호출 한도를 초과했습니다.", retry_after_seconds=wait
                )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "completed":
                raise ValueError("incomplete")
            content = [part for item in body.get("output", []) for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in content):
                raise ValueError("refused")
            texts = [part["text"] for part in content if part.get("type") == "output_text"]
            if len(texts) != 1:
                raise ValueError("missing output")
            return output_type.model_validate_json(texts[0])
        except httpx.TimeoutException:
            raise ServiceError("PROVIDER_TIMEOUT", "모델 응답 시간이 초과되었습니다.") from None
        except httpx.HTTPError:
            raise ServiceError("PROVIDER_ERROR", "모델 제공처 요청에 실패했습니다.") from None
        except ValueError, TypeError, KeyError, AttributeError:
            raise ServiceError(
                "AI_INTERPRETATION_FAILED", "모델의 구조화 응답을 검증할 수 없습니다."
            ) from None
