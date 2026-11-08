"""Current engine -> migrated storage -> historical answer, with provider facts mocked."""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import read_session_factory, write_session_factory
from app.models import (
    RecommendationCard,
    RecommendationEvidence,
    RecommendationResult,
    RecommendationSearchArea,
)
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.pending import ChatRequest
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_context_payload import prepare_snapshot
from app.services.recommendation_reason import RecommendationReasonService
from tests.fixtures.current_recommendation import current_result
from tests.fixtures.recommendation_context import seed_conditions

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["fresh", "stale", "general", "hidden_unknown"])
def test_current_engine_facts_policy_and_coverage_survive_database_round_trip(
    normalized_database, mode
):
    async def run():
        scenario = await current_result(
            traffic="stale" if mode == "stale" else "fresh",
            general=mode == "general",
            hidden_unknown=mode == "hidden_unknown",
        )
        data = scenario.data
        engine = create_async_engine(normalized_database)
        try:
            async with write_session_factory(engine).begin() as session:
                await seed_conditions(session, data)
                writer = RecommendationContextService(RecommendationContextRepository(session))
                registered = await writer.register(scenario.request.request_id, data)
            calls_before = list(scenario.source.mock_calls)
            async with read_session_factory(engine)() as session:
                reader = RecommendationContextService(RecommendationContextRepository(session))
                stored = await reader.resolve(registered.result["context_token"])
                assert stored.conditions == data.condition_update.conditions
                assert stored.result.ranking_policy == data.result["ranking_policy"]["name"]
                assert stored.result.time_bucket_seconds == 300
                assert stored.result.parking_bonus_radius_m == 500
                assert stored.result.traffic_max_age_seconds == 1800
                assert (
                    stored.result.search_more_available
                    == data.result["search_scope"]["more_available"]
                )
                assert stored.result.search_fallback is False
                assert len(stored.search_areas) == 1
                area = stored.search_areas[0]
                expected_area = data.result["search_scope"]["areas"][0]
                assert area.area_id == expected_area["area_id"]
                assert area.geometry_version == expected_area["geometry_version"]
                assert area.road_observed_at == datetime.fromisoformat(
                    expected_area["road_observed_at"]
                )
                assert area.parking_checked_at == datetime.fromisoformat(
                    expected_area["parking_checked_at"]
                )
                assert area.registered_parking_count == expected_area["registered_parking_count"]
                point = cast(RecommendationSearchArea.center, Geometry("POINT", srid=4326))
                center = (
                    await session.execute(
                        select(func.ST_X(point), func.ST_Y(point)).where(
                            RecommendationSearchArea.result_id == stored.result.result_id,
                            RecommendationSearchArea.position == 0,
                        )
                    )
                ).one()
                assert tuple(center) == (
                    expected_area["center"]["longitude"],
                    expected_area["center"]["latitude"],
                )
                for row, original in zip(stored.cards, data.result["cards"], strict=True):
                    assert row.place_id == original["place"]["value"]["place_id"]
                    if mode == "general":
                        assert row.ranking_evaluated_at is None and row.rank is None
                    else:
                        ranking = original["ranking"]
                        assert row.ranking_evaluated_at == datetime.fromisoformat(
                            ranking["evaluated_at"]
                        )
                        assert row.ranking_eta_bucket == ranking["eta_bucket"]
                        assert row.ranking_traffic_applied == ranking["traffic_applied"]
                        assert row.ranking_parking_adjustment == ranking["parking_adjustment"]
                        assert row.ranking_adjusted_grade == ranking["adjusted_traffic_grade"]
                selected = next(
                    c for c in data.result["cards"] if c["place"]["value"]["place_id"] == "kakao:c"
                )
                request = ChatRequest(
                    request_id=uuid4(),
                    trip_id=data.trip_id,
                    condition_version=0,
                    current_conditions=data.condition_update.conditions,
                    excluded_place_ids=[],
                    message="왜 추천했어?",
                    target={
                        "result_id": registered.result["result_id"],
                        "place_id": "kakao:c",
                        "context_token": registered.result["context_token"],
                    },
                )
                answer = await RecommendationReasonService(reader).answer(request)
                assert [i.model_dump(mode="json") for i in answer.answer.information] == [
                    e["information"] for e in selected["reason"]["evidence"]
                ]
                if mode == "fresh":
                    assert "한 단계 보정" in answer.message
                elif mode == "general":
                    assert "일반 후보" in answer.message and stored.evidence == ()
                else:
                    assert "보정 없이" in answer.message
                assert scenario.source.mock_calls == calls_before
                assert not session.new and not session.dirty
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_last_search_area_rolls_back_the_entire_snapshot(normalized_database):
    async def run():
        scenario = await current_result()
        data = scenario.data
        snapshot = prepare_snapshot(
            data,
            scenario.request.request_id,
            token_digest="a" * 64,
            expires_at=datetime.fromisoformat(data.result["created_at"]) + timedelta(minutes=30),
        )
        engine = create_async_engine(normalized_database)
        try:
            async with write_session_factory(engine).begin() as session:
                await seed_conditions(session, data)
                repo = RecommendationContextRepository(session)
                name = snapshot.search_areas[0].name
                snapshot.search_areas[0].name = " "
                with pytest.raises(IntegrityError):
                    await repo.save(snapshot)
                for model in (
                    RecommendationResult,
                    RecommendationCard,
                    RecommendationEvidence,
                    RecommendationSearchArea,
                ):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
                snapshot.search_areas[0].name = name
                await repo.save(snapshot)
            async with read_session_factory(engine)() as session:
                stored = await RecommendationContextRepository(session).get(
                    data.trip_id, snapshot.result.result_id
                )
                assert len(stored.cards) == 3 and len(stored.search_areas) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
