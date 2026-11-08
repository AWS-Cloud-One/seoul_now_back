"""One trip across R01, C01 and R02 using real persistence and mocked providers."""

import asyncio
from uuid import UUID

import pytest

from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from tests.integration.test_recommendation_chat_api_storage import (
    C01,
    R01,
    body,
    chat,
    counts,
    following,
    post,
    runtime,
    target,
)
from tests.integration.test_recommendation_retry_api_storage import R02, ids, retry_body

pytestmark = pytest.mark.integration


def test_trip_keeps_history_and_exclusions_across_recommendation_chat_and_retry(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            request = body()
            first = (await post(env, R01, request)).json()["data"]
            assert (await post(env, R01, request)).json()["data"] == first
            assert env.source.search.await_count == 1

            additional = (await post(env, C01, chat(following(request, first)))).json()["data"]
            assert additional["condition_update"]["version"] == 0
            assert set(ids(first)).isdisjoint(ids(additional))

            env.interpretation = Interpretation(intent="recommendation", headcount=4)
            changed = (
                await post(env, C01, chat(following(request, additional), message="네 명으로 변경"))
            ).json()["data"]
            assert changed["condition_update"]["version"] == 1
            successful = set(ids(first) + ids(additional) + ids(changed))
            assert len(successful) == 9

            env.interpretation = Interpretation(intent="recommendation", clear_fields=["headcount"])
            env.source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "경로 조회 실패")
            cleared = (
                await post(env, C01, chat(following(request, changed), message="인원 조건 해제"))
            ).json()["data"]
            assert cleared["kind"] == "general_candidates"
            assert cleared["condition_update"]["conditions"]["headcount"] is None
            assert cleared["condition_update"]["version"] == 2
            assert successful.isdisjoint(ids(cleared))
            entry = env.app.state.recommendation_progress.trip(UUID(request["trip_id"]))
            assert entry.excluded == successful

            env.interpretation = Interpretation(intent="recommendation_reason")
            provider_calls = list(env.source.mock_calls)
            answer = (
                await post(env, C01, chat(following(request, cleared), target=target(first)))
            ).json()["data"]
            assert not answer["condition_update"]["applied"]
            assert answer["condition_update"]["version"] == 2
            assert answer["condition_update"]["conditions"]["headcount"] is None
            assert "인원 2명" in answer["message"]
            assert answer["answer"]["original_result_id"] == first["result"]["result_id"]
            assert answer["answer"]["information"] == [
                evidence["information"]
                for evidence in first["result"]["cards"][0]["reason"]["evidence"]
            ]
            assert env.source.mock_calls == provider_calls

            env.source.route.side_effect = None
            retry = retry_body(request, cleared)
            searches = env.source.search.await_count
            recovered = (await post(env, R02, retry)).json()["data"]
            assert recovered["kind"] == "recommendation"
            assert set(ids(recovered)) == set(ids(cleared))
            assert recovered["condition_update"] == {
                "applied": True,
                "conditions": cleared["condition_update"]["conditions"],
                "version": 2,
                "changes": [],
            }
            assert env.source.search.await_count == searches
            calls = list(env.source.mock_calls), list(env.model.mock_calls)
            assert (await post(env, R02, retry)).json()["data"] == recovered
            assert (env.source.mock_calls, env.model.mock_calls) == calls
            assert entry.excluded == successful | set(ids(recovered))
            assert await counts(env) == (1, 3, 5)

            async with env.app.state.session_factory() as session:
                conditions = RecommendationConditionsRepository(session)
                trip = UUID(request["trip_id"])
                assert (await conditions.get_current(trip)).version == 2
                assert [(await conditions.get_version(trip, i)).headcount for i in range(3)] == [
                    2,
                    4,
                    None,
                ]
                contexts = RecommendationContextRepository(session)
                saved = await contexts.get(trip, UUID(recovered["result"]["result_id"]))
                source = await contexts.get(trip, UUID(cleared["result"]["result_id"]))
                assert saved.result.source_result_id == source.result.result_id
                assert source.result.kind == "general_candidates"
                assert all(card.duration_seconds is None for card in source.cards)

    asyncio.run(run())
