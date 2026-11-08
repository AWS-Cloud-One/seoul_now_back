import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.dependencies import get_ai_provider, get_kakao_local_client, get_recommendation_service
from app.core.config import Settings
from app.db.session import write_session_factory
from app.main import create_app
from app.modules.ai.interpretation import Interpretation
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.recommendation_context import response, seed_conditions

pytestmark = pytest.mark.integration


def test_http_chat_reason_resolves_persisted_context_before_interpretation(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        data = response()
        data.result["created_at"] = datetime.now(UTC).isoformat()
        try:
            async with write_session_factory(engine).begin() as session:
                await seed_conditions(session, data)
                registered = await RecommendationContextService(
                    RecommendationContextRepository(session)
                ).register(uuid4(), data)
        finally:
            await engine.dispose()

        app = create_app(Settings(_env_file=None, database_url=normalized_database))
        model, kakao, recommendations = AsyncMock(), AsyncMock(), AsyncMock()
        model.complete.return_value = Interpretation(intent="recommendation_reason")
        app.dependency_overrides[get_ai_provider] = lambda: model
        app.dependency_overrides[get_kakao_local_client] = lambda: kakao
        app.dependency_overrides[get_recommendation_service] = lambda: recommendations
        body = {
            "request_id": str(uuid4()),
            "trip_id": str(data.trip_id),
            "condition_version": 5,
            "current_conditions": data.condition_update.conditions.model_dump(mode="json"),
            "excluded_place_ids": ["kakao:original"],
            "message": "왜 추천했어?",
            "target": {
                "result_id": registered.result["result_id"],
                "place_id": "kakao:original",
                "context_token": registered.result["context_token"],
            },
        }
        body["current_conditions"]["headcount"] = 7
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                reply = await client.post("/api/v1/chat/messages", json=body)
                assert reply.status_code == 200
                answer = reply.json()["data"]
                assert answer["kind"] == "answer" and "인원 2명" in answer["message"]
                assert answer["condition_update"]["conditions"]["headcount"] == 7
                assert answer["condition_update"]["version"] == 5
                assert answer["answer"]["information"][0]["value"]["duration_seconds"] == 600
                assert answer["answer"]["checked_at"] is None
                assert answer["stages"]["interpretation"] == "ok"
                assert registered.result["context_token"] not in reply.text
                assert reply.headers["cache-control"] == "no-store"
                body["target"]["place_id"] = "kakao:other"
                invalid = await client.post("/api/v1/chat/messages", json=body)
                assert invalid.status_code == 422
                assert invalid.json()["error"]["code"] == "INVALID_CONTEXT"
                model.complete.assert_awaited_once()
                recommendations.execute.assert_not_awaited()
                kakao.keyword.assert_not_awaited()

    asyncio.run(run())
