import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.exceptions import ServiceError
from app.modules.recommendation.types import Candidate
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.pending import ChatRequest
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_reason import RecommendationReasonService
from tests.fixtures.current_recommendation import current_result


async def register(scenario):
    repo = AsyncMock(spec=RecommendationContextRepository)
    repo.get.return_value = None
    service = RecommendationContextService(repo)
    registered = await service.register(scenario.request.request_id, scenario.data)
    stored = repo.save.call_args.args[0]
    repo.get_by_token_digest.return_value = stored
    return service, registered, stored


def reason_request(scenario, registered, place="kakao:c"):
    return ChatRequest(
        request_id=uuid4(),
        trip_id=scenario.request.trip_id,
        condition_version=0,
        current_conditions=scenario.request.current_conditions,
        excluded_place_ids=[],
        message="왜 추천했어?",
        target={
            "result_id": registered.result["result_id"],
            "place_id": place,
            "context_token": registered.result["context_token"],
        },
    )


def test_engine_result_retains_actual_grade_adjustment_and_compact_search_scope():
    async def run():
        scenario = await current_result()
        service, registered, stored = await register(scenario)
        assert [c.place_id for c in stored.cards] == ["kakao:b", "kakao:c", "kakao:a"]
        c = stored.cards[1]
        assert c.ranking_adjusted_grade == "서행" and c.ranking_parking_adjustment == 1
        traffic = next(
            e for e in stored.evidence if e.type == "traffic" and e.place_id == c.place_id
        )
        assert traffic.traffic_grade == "정체" and traffic.status == "available"
        assert traffic.source == "verified-road" and traffic.observed_at is not None
        assert stored.result.time_bucket_seconds == 300
        assert stored.result.parking_bonus_radius_m == 500
        assert stored.result.search_fallback is False
        area = stored.search_areas[0]
        assert area.area_id == "1101053" and area.geometry_version == "2026-v1"
        assert area.registered_parking_count == 7
        assert "rings" not in registered.result["search_scope"]["areas"][0]
        answer = await RecommendationReasonService(service).answer(
            reason_request(scenario, registered)
        )
        assert "5분" in answer.message and "한 단계 보정" in answer.message
        assert next(i.value for i in answer.answer.information if i.source == "verified-road") == {
            "grade": "정체"
        }
        assert not answer.condition_update.applied

    asyncio.run(run())


def test_unshown_unknown_candidate_preserves_whole_bucket_fallback():
    async def run():
        scenario = await current_result(hidden_unknown=True)
        service, registered, stored = await register(scenario)
        assert [c.place_id for c in stored.cards] == ["kakao:a", "kakao:b", "kakao:c"]
        assert all(not c.ranking_traffic_applied for c in stored.cards)
        assert all(c.ranking_adjusted_grade is None for c in stored.cards)
        assert all(c.ranking_parking_adjustment == 0 for c in stored.cards)
        assert all(e.status == "available" for e in stored.evidence if e.type == "traffic")
        answer = await RecommendationReasonService(service).answer(
            reason_request(scenario, registered)
        )
        assert "미확인 후보" in answer.message and "보정 없이" in answer.message
        assert stored.result.search_more_available is True

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["missing", "stale", "future"])
def test_unknown_traffic_is_retained_without_fabricating_a_grade(mode):
    async def run():
        scenario = await current_result(traffic=mode, areas=False)
        service, registered, stored = await register(scenario)
        traffic = [e for e in stored.evidence if e.type == "traffic"]
        assert all(e.status == "unavailable" and e.traffic_grade is None for e in traffic)
        assert all(e.reason_code for e in traffic)
        if mode == "stale":
            assert all(e.freshness == "stale" and e.reason_code == "STALE_DATA" for e in traffic)
        assert stored.result.search_fallback is True and stored.search_areas == ()
        answer = await RecommendationReasonService(service).answer(
            reason_request(scenario, registered)
        )
        assert any(i.value is None and i.status == "unavailable" for i in answer.answer.information)
        assert "한 단계 보정" not in answer.message

    asyncio.run(run())


def test_general_candidates_store_coverage_without_ranking_or_evidence():
    async def run():
        scenario = await current_result(general=True)
        service, registered, stored = await register(scenario)
        assert stored.result.kind == "general_candidates"
        assert stored.evidence == () and len(stored.search_areas) == 1
        assert all(c.rank is None and c.ranking_evaluated_at is None for c in stored.cards)
        answer = await RecommendationReasonService(service).answer(
            reason_request(scenario, registered)
        )
        assert "일반 후보" in answer.message and answer.answer.information == []

    asyncio.run(run())


def test_route_retry_result_has_policy_but_does_not_invent_a_search_scope():
    async def run():
        scenario = await current_result(general=True)
        original = [
            Candidate(
                place=c["place"]["value"],
                collected_at=c["place"]["collected_at"],
            )
            for c in scenario.data.result["cards"]
        ]
        scenario.source.route.side_effect = lambda origin, destination: (600, 1000)
        request = scenario.request.model_copy(update={"request_id": uuid4()})
        retried = await scenario.service.retry_routes(request, original)
        assert retried.failure is None and retried.response.kind == "recommendation"
        scenario.data, scenario.request = retried.response, request
        _, _, stored = await register(scenario)
        assert stored.result.ranking_policy == "eta_traffic_parking_v1"
        assert stored.result.search_fallback is None and stored.result.search_more_available is None
        assert stored.search_areas == ()
        assert scenario.source.search.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "adjustment",
        "bucket",
        "decision_time",
        "traffic_time",
        "missing_source",
        "unknown_with_value",
        "missing_decision",
        "missing_policy",
        "mixed_bucket",
        "order",
        "duplicate_area",
        "area_fallback",
        "extra_field",
        "wrong_fact_shape",
    ],
)
def test_inconsistent_current_results_are_rejected_before_repository_access(fault):
    async def run():
        scenario = await current_result()
        data = scenario.data
        card = data.result["cards"][1]
        evidence = next(e for e in card["reason"]["evidence"] if e["type"] == "traffic")
        if fault == "adjustment":
            card["ranking"]["parking_adjustment"] = 0
        elif fault == "bucket":
            card["ranking"]["eta_bucket"] += 1
        elif fault == "decision_time":
            card["ranking"]["evaluated_at"] = (
                datetime.fromisoformat(data.result["created_at"]) + timedelta(seconds=1)
            ).isoformat()
        elif fault == "traffic_time":
            evidence["information"]["observed_at"] = (
                datetime.fromisoformat(data.result["created_at"]) - timedelta(hours=1)
            ).isoformat()
        elif fault == "missing_source":
            evidence["information"]["source"] = None
        elif fault == "unknown_with_value":
            evidence["information"]["status"] = "unavailable"
        elif fault == "missing_decision":
            card.pop("ranking")
        elif fault == "missing_policy":
            data.result.pop("ranking_policy")
        elif fault == "mixed_bucket":
            card["ranking"].update(
                traffic_applied=False, parking_adjustment=0, adjusted_traffic_grade=None
            )
        elif fault == "order":
            data.result["cards"].reverse()
            for rank, item in enumerate(data.result["cards"], 1):
                item["rank"] = rank
        elif fault == "duplicate_area":
            data.result["search_scope"]["areas"] *= 2
        elif fault == "area_fallback":
            data.result["search_scope"]["fallback"] = True
        elif fault == "extra_field":
            data.result["unrecognized"] = "must not be dropped"
        else:
            evidence["information"]["value"] = {"distance_meters": 10}
        repo = AsyncMock(spec=RecommendationContextRepository)
        with pytest.raises(ServiceError) as error:
            await RecommendationContextService(repo).register(scenario.request.request_id, data)
        assert error.value.code == "INTERNAL_ERROR"
        repo.get.assert_not_awaited()
        repo.save.assert_not_awaited()

    asyncio.run(run())
