import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from app.core.exceptions import ServiceError
from app.modules.recommendation.cards import unavailable_parking
from app.modules.recommendation.engine import DestinationRecommendationEngine
from app.modules.recommendation.progress import MemoryProgressStore
from app.modules.recommendation.types import Candidate
from app.schemas.recommendations import RecommendationRequest
from app.services.recommendation_adapters import RecommendationConditionsValidator
from app.services.recommendation_execution import RecommendationExecution
from app.services.recommendations import RecommendationService


def request():
    return RecommendationRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "trip_id": str(uuid4()),
            "condition_version": 0,
            "current_conditions": {
                "origin": {
                    "label": "서울",
                    "coordinates": {"latitude": 37.56, "longitude": 126.97},
                    "address": None,
                    "region": "seoul",
                    "supported": True,
                },
                "purposes": [{"purpose": "cafe", "keyword": None}],
                "group_type": None,
                "age_groups": None,
                "headcount": None,
                "max_travel_seconds": None,
            },
            "excluded_place_ids": [],
            "conditions_patch": {},
        }
    )


def candidate(i):
    return Candidate.model_validate(
        {
            "place": {
                "place_id": str(i),
                "name": str(i),
                "purpose": "cafe",
                "category_code": "CE7",
                "category_name": None,
                "coordinates": {"latitude": 37.57, "longitude": 126.98},
                "address": None,
                "road_address": None,
                "phone": None,
                "provider_url": None,
            },
            "collected_at": datetime.now(UTC),
        }
    )


def setup(store=None):
    source = AsyncMock()
    source.region.return_value = "seoul"
    source.route.return_value = (600, 1000)
    source.parking.return_value = unavailable_parking()
    source.search.side_effect = lambda q, o, purpose, page: (
        [candidate(i) for i in range((page - 1) * 15, page * 15)],
        page == 5,
    )
    engine = DestinationRecommendationEngine(source)
    return (
        RecommendationService(
            RecommendationConditionsValidator(source),
            engine,
            execution=RecommendationExecution(store or MemoryProgressStore()),
        ),
        source,
        engine,
    )


def next_request(req, outcome):
    return req.model_copy(
        update={
            "request_id": uuid4(),
            "current_conditions": outcome.response.condition_update.conditions,
            "condition_version": outcome.response.condition_update.version,
            "conditions_patch": req.conditions_patch.__class__(),
        },
        deep=True,
    )


def ids(outcome):
    return [c["place"]["value"]["place_id"] for c in outcome.response.result["cards"]]


def test_leftovers_then_pages_and_same_request_replay():
    async def run():
        service, source, _ = setup()
        req = request()
        first = await service.execute(req)
        duplicate = await service.execute(req)
        assert first.response == duplicate.response
        assert source.search.await_count == 1
        seen = set(ids(first))
        for _ in range(6):
            req = next_request(req, first)
            first = await service.execute(req)
            assert not seen.intersection(ids(first))
            seen.update(ids(first))
        assert source.search.await_count == 2
        assert len(seen) == 21

    asyncio.run(run())


def test_concurrent_replay_conflict_and_version():
    async def run():
        service, source, _ = setup()
        req = request()
        a, b = await asyncio.gather(service.execute(req), service.execute(req))
        assert a.response == b.response and source.search.await_count == 1
        changed = req.model_copy(deep=True)
        changed.excluded_place_ids = ["x"]
        assert (await service.execute(changed)).failure.code == "REQUEST_ID_CONFLICT"
        changed = next_request(req, a)
        changed.conditions_patch.headcount = 2
        accepted = await service.execute(changed)
        stale = next_request(req, a)
        assert (await service.execute(stale)).failure.code == "CONTEXT_CONDITIONS_MISMATCH"
        assert accepted.response.condition_update.version == 1

    asyncio.run(run())


def test_changes_keep_history_and_failed_request_is_cached():
    async def run():
        service, source, _ = setup()
        req = request()
        first = await service.execute(req)
        req = next_request(req, first)
        req.conditions_patch.headcount = 3
        source.search.side_effect = ServiceError("RATE_LIMITED", "limit")
        failure = await service.execute(req)
        assert failure.response.condition_update.applied
        calls = source.search.await_count
        assert (await service.execute(req)).response == failure.response
        assert source.search.await_count == calls
        source.search.side_effect = None
        source.search.return_value = ([candidate(i) for i in range(15)], True)
        retry = await service.execute(next_request(req, failure))
        assert not set(ids(first)).intersection(ids(retry))

    asyncio.run(run())


def test_expiry_refuses_to_silently_restart_existing_trip():
    async def run():
        clock = [0.0]
        store = MemoryProgressStore(ttl_seconds=10, clock=lambda: clock[0])
        service, source, _ = setup(store)
        req = request()
        first = await service.execute(req)
        clock[0] = 11
        outcome = await service.execute(next_request(req, first))
        assert outcome.failure.code == "CONTEXT_EXPIRED"
        assert source.search.await_count == 1
        assert (await service.execute(request())).failure is None

    asyncio.run(run())


def test_retry_routes_only_original_candidates_and_success_history_once():
    async def run():
        service, source, _ = setup()
        req = request()
        source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "timeout")
        source.search.side_effect = None
        source.search.return_value = ([candidate(i) for i in range(3)], True)
        first = await service.execute(req)
        assert first.response.kind == "general_candidates"
        retry = next_request(req, first)
        retry.excluded_place_ids = ["0"]
        source.route.side_effect = None
        original = [candidate(i) for i in range(3)]
        outcome = await service.retry_routes(retry, original)
        assert ids(outcome) == ["1", "2"]
        calls = source.route.await_count
        again = await service.retry_routes(retry, original)
        assert again.response == outcome.response and source.route.await_count == calls
        assert source.search.await_count == 1

    asyncio.run(run())


def test_request_budget_continues_at_page_four_and_trip_isolation():
    async def run():
        service, source, _ = setup()
        source.search.side_effect = lambda q, o, purpose, page: (
            [] if page < 4 else [candidate("late")],
            page == 4,
        )
        req = request()
        first = await service.execute(req)
        assert first.response.kind == "no_candidates"
        assert "SEARCH_INCOMPLETE" in [w.code for w in first.response.warnings]
        second = await service.execute(next_request(req, first))
        assert ids(second) == ["late"]
        assert [c.args[-1] for c in source.search.await_args_list] == [1, 2, 3, 4]
        await service.execute(request())
        assert source.search.await_args_list[4].args[-1] == 1

    asyncio.run(run())


def test_retry_failure_and_time_limit_do_not_turn_into_success_history():
    async def run():
        service, source, engine = setup()
        req = request()
        req.current_conditions.max_travel_seconds = 600
        originals = [candidate(1), candidate(2)]
        source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "timeout")
        failed = await service.retry_routes(req, originals)
        assert failed.response.kind == "general_candidates"
        assert not service.execution.store.trip(req.trip_id).excluded
        source.route.side_effect = [(601, 10), ServiceError("PROVIDER_TIMEOUT", "timeout")]
        outcome = await service.retry_routes(next_request(req, failed), originals)
        assert outcome.response.kind == "no_candidates"
        assert outcome.response.stages.routes == "partial"
        assert not service.execution.store.trip(req.trip_id).excluded
        source.search.assert_not_awaited()

    asyncio.run(run())


def test_snapshot_is_immutable_and_unknown_or_stale_congestion_not_ranked():
    async def run():
        from datetime import timedelta

        service, source, _ = setup()
        req = request()
        values = [candidate(1), candidate(2)]
        values[0].congestion = "정체"
        values[1].congestion = "원활"
        for c in values:
            c.congestion_source = "verified-roads"
            c.congestion_observed_at = datetime.now(UTC)
        source.search.side_effect = None
        source.search.return_value = (values, True)
        source.route.side_effect = [(600, 10), (650, 20)]
        first = await service.execute(req)
        assert ids(first) == ["2", "1"]
        first.response.result["cards"].clear()
        replay = await service.execute(req)
        assert ids(replay) == ["2", "1"]
        for c in values:
            c.congestion_observed_at -= timedelta(minutes=31)
        source.route.side_effect = [(600, 10), (650, 20)]
        assert ids(await service.execute(request())) == ["1", "2"]

    asyncio.run(run())


def test_explicit_null_and_omitted_patch_and_noop_version():
    async def run():
        service, _, _ = setup()
        req = request()
        req.current_conditions.max_travel_seconds = 900
        first = await service.execute(req)
        req = next_request(req, first)
        req.conditions_patch.headcount = 2
        second = await service.execute(req)
        assert second.response.condition_update.conditions.max_travel_seconds == 900
        req = next_request(req, second)
        req.conditions_patch.headcount = 2
        same = await service.execute(req)
        assert same.response.condition_update.version == second.response.condition_update.version
        req = next_request(req, same)
        req.conditions_patch.max_travel_seconds = None
        cleared = await service.execute(req)
        assert cleared.response.condition_update.conditions.max_travel_seconds is None

    asyncio.run(run())


def test_same_id_omission_and_explicit_null_are_different_requests():
    async def run():
        service, source, _ = setup()
        req = request()
        req.current_conditions.max_travel_seconds = 900
        await service.execute(req)
        changed = req.model_copy(deep=True)
        changed.conditions_patch.max_travel_seconds = None
        conflict = await service.execute(changed)
        assert conflict.failure.code == "REQUEST_ID_CONFLICT"
        assert source.search.await_count == 1

    asyncio.run(run())
