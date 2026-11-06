"""Async Responses API over existing HTTPX; no paid call on import/startup."""

import json

import httpx
from pydantic import ValidationError

from app.modules.recommendation.errors import ProviderError


def strict_schema(schema):
    if isinstance(schema, dict):
        schema = {k: strict_schema(v) for k, v in schema.items() if k != "default"}
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
            schema["required"] = list(schema.get("properties", {}))
    elif isinstance(schema, list):
        schema = [strict_schema(v) for v in schema]
    return schema


class OpenAIProvider:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    async def complete(self, instructions, payload, output_type):
        key = self.settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ProviderError(503, "openai_not_configured", "APP_OPENAI_API_KEY를 설정해 주세요.")
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.settings.openai_model,
                    "store": False,
                    "instructions": instructions,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "max_output_tokens": 1200,
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
            if response.status_code in (401, 403):
                raise ProviderError(
                    502, "openai_auth", "OpenAI API 키와 모델 접근 권한을 확인해 주세요."
                )
            if response.status_code == 429:
                raise ProviderError(
                    503, "openai_quota", "OpenAI 사용 한도 또는 결제 설정을 확인해 주세요."
                )
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "completed":
                raise ValueError("incomplete response")
            content = [part for item in data.get("output", []) for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in content):
                raise ValueError("refused")
            texts = [part["text"] for part in content if part.get("type") == "output_text"]
            if len(texts) != 1:
                raise ValueError("missing output")
            return output_type.model_validate_json(texts[0])
        except httpx.TimeoutException:
            raise ProviderError(
                504, "openai_timeout", "OpenAI 응답 시간이 초과되었습니다."
            ) from None
        except httpx.HTTPError:
            raise ProviderError(502, "openai_unavailable", "OpenAI 요청에 실패했습니다.") from None
        except ValueError, TypeError, KeyError, AttributeError, ValidationError:
            raise ProviderError(
                502, "openai_invalid", "모델의 구조화 응답을 검증할 수 없습니다."
            ) from None
