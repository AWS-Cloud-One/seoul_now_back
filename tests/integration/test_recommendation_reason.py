import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.dependencies import get_context_reader, get_recommendation_reason_service
from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.db.session import read_session_factory, write_session_factory
from app.models import ChatSession, Place
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.pending import ChatRequest
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.recommendation_context import NOW, response, seed_conditions

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("general", [False, True])
def test_reason_reads_original_snapshot_after_conditions_and_place_cache_change(
    normalized_database,
    general,
):
    async def run():
        engine = create_async_engine(normalized_database)
        readers, writers = read_session_factory(engine), write_session_factory(engine)
        original = response(general=general)
        current = original.model_copy(deep=True)
        current.condition_update.conditions.origin.label = "변경한 출발지"
        current.condition_update.conditions.headcount = 7
        current.condition_update.conditions.max_travel_seconds = 3600
        try:
            async with writers.begin() as session:
                await seed_conditions(session, original)
                contexts = RecommendationContextService(
                    RecommendationContextRepository(session),
                    clock=lambda: NOW,
                )
                registered = await contexts.register(uuid4(), original)
            async with writers.begin() as session:
                await seed_conditions(session, current, version=1, new_trip=False)
                await session.execute(
                    update(ChatSession)
                    .where(ChatSession.trip_id == original.trip_id)
                    .values(condition_version=1)
                )
                await session.execute(
                    Place.__table__.insert().values(
                        place_id="kakao:original",
                        name="최신 캐시 이름",
                        location="SRID=4326;POINT(128 38)",
                        collected_at=NOW + timedelta(minutes=1),
                        expires_at=NOW + timedelta(days=1),
                    )
                )
            async with readers() as session:
                contexts = get_context_reader(session, Settings(_env_file=None))
                contexts.clock = lambda: NOW + timedelta(minutes=2)
                service = get_recommendation_reason_service(contexts)
                request = ChatRequest(
                    request_id=uuid4(),
                    trip_id=original.trip_id,
                    condition_version=1,
                    current_conditions=current.condition_update.conditions,
                    excluded_place_ids=["kakao:original"],
                    message="왜 추천했어?",
                    target={
                        "result_id": registered.result["result_id"],
                        "place_id": "kakao:original",
                        "context_token": registered.result["context_token"],
                    },
                )
                answer = await service.answer(request)
                assert await session.scalar(text("SHOW transaction_read_only")) == "on"
                assert "당시 카페" in answer.message and "최신 캐시 이름" not in answer.message
                assert "변경한 출발지" not in answer.message
                assert answer.condition_update.conditions == request.current_conditions
                assert answer.condition_update.version == 1 and not answer.condition_update.applied
                assert answer.answer.original_recommended_at == NOW
                assert answer.answer.checked_at is None
                if general:
                    assert "일반 후보" in answer.message and answer.answer.information == []
                else:
                    assert "서울역" in answer.message and "인원 2명" in answer.message
                    route, parking = answer.answer.information
                    assert route.value["duration_seconds"] == 600 and route.collected_at == NOW
                    assert route.observed_at is None and parking.collected_at is None
                assert not session.new and not session.dirty
                contexts.clock = lambda: NOW + timedelta(minutes=30)
                with pytest.raises(ServiceError) as error:
                    await service.answer(request)
                assert error.value.code == "CONTEXT_EXPIRED"
        finally:
            await engine.dispose()

    asyncio.run(run())
