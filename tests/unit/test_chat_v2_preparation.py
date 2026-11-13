"""Use the real recommendation engine; only its external data ports are doubles."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ServiceError
from app.modules.chat_v2.preparation import ExistingRecommendationPreparer
from app.modules.chat_v2.results import PreparedRecommendation
from app.modules.chat_v2.validation import StageFailure
from app.modules.recommendation.progress import RecommendationWork, SearchProgress
from tests.fixtures.chat_v2 import scenario
from tests.unit.recommendation.test_geography import area
from tests.unit.recommendation.test_progress import candidate, setup


def preparation_case():
    _, _, prepared, _ = scenario()
    plan = prepared.plan
    service, source, engine = setup()
    engine.area_provider = AsyncMock()
    known_area = area()
    known_area.congestion = "서행"
    known_area.road_observed_at = datetime.now(UTC)
    engine.area_provider.areas.return_value = [known_area]
    source.search.return_value = ([candidate("candidate-1")], True)
    source.search.side_effect = None
    work = RecommendationWork(
        version=plan.context.condition_version,
        conditions=plan.context.current_conditions.model_copy(deep=True),
        excluded={"previously-shown"},
        search=SearchProgress(),
        commit_hook=AsyncMock(),
    )
    service.execute = AsyncMock(side_effect=AssertionError("Must never publish"))
    adapter = ExistingRecommendationPreparer(service, identity=plan.context.identity, work=work)
    return plan, adapter, source


def test_actual_engine_preparation_keeps_evidence_but_never_claims_commit():
    plan, adapter, source = preparation_case()
    source.parking.return_value.next_cursor = "preparation-local-cursor"
    result = asyncio.run(adapter.prepare(plan))
    assert result.outcome == "recommendation" and len(result.cards) == 1
    assert result.cards[0].route.value.duration_seconds == 600
    assert result.cards[0].parking.next_cursor is None
    assert result.ranking_policy.name == "eta_traffic_parking_v1"
    assert result.search_scope.areas[0].area_id == "dong"
    assert "EXPLANATION_FALLBACK" in {warning.code for warning in result.warnings}
    assert not {"result_id", "applied", "context_token"}.intersection(result.model_dump())
    assert adapter.work.conditions.headcount == 2 and adapter.work.version == 7
    assert adapter.work.excluded == {"previously-shown"}
    adapter.work.commit_hook.assert_not_awaited()
    adapter.service.execute.assert_not_awaited()
    assert source.search.await_count == 1
    assert PreparedRecommendation.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "ended,expected", [(True, "no_candidates"), (False, "insufficient_evidence")]
)
def test_empty_search_distinguishes_finished_scope_from_unfinished_search(ended, expected):
    plan, adapter, source = preparation_case()
    source.search.return_value = ([], ended)
    result = asyncio.run(adapter.prepare(plan))
    assert result.outcome == expected and result.cards == []
    assert result.search_scope.more_available is not ended


@pytest.mark.parametrize("port", ["route", "search"])
def test_provider_failure_does_not_become_no_candidates(port):
    plan, adapter, source = preparation_case()
    getattr(source, port).side_effect = ServiceError("PROVIDER_TIMEOUT", "제공처 응답 시간 초과")
    result = asyncio.run(adapter.prepare(plan))
    assert result.outcome == "error" and result.error.code == "RECOMMENDATION_FAILED"
    assert result.cards == []
    adapter.work.commit_hook.assert_not_awaited()


def test_stale_workspace_is_rejected_before_any_query():
    plan, adapter, source = preparation_case()
    adapter.work.version -= 1
    with pytest.raises(StageFailure) as caught:
        asyncio.run(adapter.prepare(plan))
    assert caught.value.error.code == "CONFLICT"
    source.region.assert_not_awaited()
    source.search.assert_not_awaited()


def test_revalidation_cannot_silently_change_another_condition():
    plan, adapter, source = preparation_case()
    source.region.return_value = "gyeonggi"
    with pytest.raises(StageFailure) as caught:
        asyncio.run(adapter.prepare(plan))
    assert caught.value.error.code == "INTERNAL_ERROR"
    assert plan.context.current_conditions.origin.region == "seoul"
