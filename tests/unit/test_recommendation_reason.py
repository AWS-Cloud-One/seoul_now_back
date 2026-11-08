import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.dependencies import get_recommendation_reason_service
from app.core.exceptions import ServiceError
from app.schemas.pending import ChatRequest
from app.services.pending import PendingService
from app.services.recommendation_context import RecommendationContextService
from app.services.recommendation_context_payload import prepare_snapshot
from tests.fixtures.recommendation_context import NOW, response


def scenario(data=None):
    data = data or response()
    stored = prepare_snapshot(
        data,
        uuid4(),
        token_digest="a" * 64,
        expires_at=NOW + timedelta(minutes=30),
    )
    current = data.condition_update.conditions.model_copy(deep=True)
    current.origin.label = "현재 출발지"
    current.headcount = 5
    current.max_travel_seconds = 3600
    request = ChatRequest(
        request_id=uuid4(),
        trip_id=data.trip_id,
        condition_version=9,
        current_conditions=current,
        excluded_place_ids=["kakao:original"],
        message="왜 추천했어?",
        target={
            "result_id": data.result["result_id"],
            "place_id": "kakao:original",
            "context_token": "opaque-token",
        },
    )
    contexts = AsyncMock(spec=RecommendationContextService)
    contexts.resolve.return_value = stored
    return get_recommendation_reason_service(contexts), contexts, request, stored


@pytest.mark.parametrize("source", ["ai", "fallback"])
def test_answer_uses_original_conditions_and_explanation_without_changing_current_state(source):
    async def run():
        data = response()
        data.result["cards"][0]["reason"]["source"] = source
        data.stages.explanation = "ok" if source == "ai" else "fallback"
        service, contexts, request, stored = scenario(data)
        original_request = request.model_dump()
        result = await service.answer(request)
        contexts.resolve.assert_awaited_once_with(
            request.target.context_token,
            trip_id=request.trip_id,
            result_id=request.target.result_id,
            place_id=request.target.place_id,
        )
        assert result.kind == "answer" and result.result is None
        assert result.answer.topic == "recommendation_reason"
        assert result.answer.text == result.message
        assert "서울역" in result.message and "인원 2명" in result.message
        assert "검색어: 조용한" in result.message and "1800초" in result.message
        assert "현재 출발지" not in result.message
        assert result.message.endswith(stored.cards[0].reason_text)
        assert result.answer.original_result_id == request.target.result_id
        assert result.answer.original_recommended_at == NOW
        assert result.answer.checked_at is None
        assert result.stages.model_dump() == {
            "interpretation": "skipped",
            "routes": "skipped",
            "explanation": "skipped",
        }
        assert result.condition_update.applied is False
        assert result.condition_update.conditions == request.current_conditions
        assert result.condition_update.version == result.base_condition_version == 9
        assert result.condition_update.changes == []
        # Check the actual pending adapter contract without wiring every targeted question here.
        PendingService.validate_context(result, request, retry=False)
        assert request.model_dump() == original_request
        result.condition_update.conditions.headcount = 10
        assert request.current_conditions.headcount == 5 and stored.conditions.headcount == 2
        contexts.register.assert_not_awaited()
        contexts.validate_retry.assert_not_awaited()

    asyncio.run(run())


def test_provenance_unknown_times_and_historical_freshness_are_preserved():
    async def run():
        service, _, request, stored = scenario()
        eta = stored.evidence[0]
        eta.freshness = stored.cards[0].route_freshness = "stale"
        eta.observed_at = stored.cards[0].route_observed_at = NOW - timedelta(minutes=20)
        result = await service.answer(request)
        route, parking = result.answer.information
        assert route.value == {"duration_seconds": 600, "distance_meters": 1234.5}
        assert route.freshness == "stale" and route.observed_at == eta.observed_at
        assert route.collected_at == NOW and route.source == "kakao_mobility"
        assert parking.value == {"distance_meters": 95.25}
        assert (
            parking.observed_at is None and parking.collected_at is None and parking.source is None
        )
        assert result.answer.checked_at is None
        assert '"distance_meters":95.25' in result.model_dump_json()

    asyncio.run(run())


def test_only_selected_cards_evidence_is_returned_in_original_order():
    async def run():
        data = response()
        other = deepcopy(data.result["cards"][0])
        other["rank"] = 2
        other["place"]["value"]["place_id"] = "kakao:second"
        other["place"]["value"]["name"] = "두 번째 카페"
        other["reason"]["text"] = "두 번째 카드의 당시 설명"
        other["route"]["value"]["duration_seconds"] = 900
        other["reason"]["evidence"][0]["information"]["value"]["duration_seconds"] = 900
        data.result["cards"].append(other)
        service, contexts, request, stored = scenario(data)
        contexts.resolve.return_value = replace(stored, evidence=tuple(reversed(stored.evidence)))
        request.target.place_id = "kakao:second"
        result = await service.answer(request)
        assert result.answer.place_id == "kakao:second"
        assert result.message.endswith(other["reason"]["text"])
        assert len(result.answer.information) == 2
        assert result.answer.information[0].value["duration_seconds"] == 900
        assert result.answer.information[1].value == {"distance_meters": 95.25}

    asyncio.run(run())


def test_general_candidate_gets_an_explanation_of_its_unconfirmed_status():
    async def run():
        service, contexts, request, _ = scenario(response(general=True))
        result = await service.answer(request)
        assert "일반 후보" in result.message and "확정된 추천 이유는 없습니다" in result.message
        assert result.answer.information == [] and result.answer.checked_at is None
        assert result.answer.original_recommended_at == NOW
        assert result.condition_update.applied is False
        contexts.register.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("code", ["INVALID_CONTEXT", "CONTEXT_EXPIRED", "DATA_UNAVAILABLE"])
def test_context_errors_are_propagated_without_a_fabricated_answer(code):
    async def run():
        service, contexts, request, _ = scenario()
        failure = ServiceError(code, "문맥 검증 실패", retryable=False)
        contexts.resolve.side_effect = failure
        with pytest.raises(ServiceError) as error:
            await service.answer(request)
        assert error.value is failure
        contexts.register.assert_not_awaited()

    asyncio.run(run())


def test_missing_target_requires_selection_without_looking_up_a_recent_result():
    async def run():
        service, contexts, request, _ = scenario()
        request.target = None
        with pytest.raises(ServiceError) as error:
            await service.answer(request)
        assert error.value.code == "INVALID_CONTEXT"
        contexts.resolve.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["missing", "eta_value", "eta_source", "parking_value", "unknown_kind"]
)
def test_incomplete_or_inconsistent_stored_evidence_is_not_presented_as_a_reason(fault):
    async def run():
        service, contexts, request, stored = scenario()
        if fault == "missing":
            contexts.resolve.return_value = replace(stored, evidence=())
        elif fault == "eta_value":
            stored.evidence[0].duration_seconds += 1
        elif fault == "eta_source":
            stored.evidence[0].source = "different-source"
        elif fault == "parking_value":
            stored.evidence[1].distance_meters += 1
        else:
            stored.evidence[1].type = "population"
        with pytest.raises(ServiceError) as error:
            await service.answer(request)
        assert error.value.code == "DATA_UNAVAILABLE"

    asyncio.run(run())
