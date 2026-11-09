"""R02 through real API wiring and isolated PostgreSQL, with external providers mocked."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.api.dependencies import get_context_reader
from app.core.exceptions import ServiceError
from app.models import ChatSession, RecommendationCard, RecommendationResult
from app.modules.ai.interpretation import Interpretation
from app.modules.recommendation.progress import MemoryProgressStore
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_context import RecommendationContextService
from tests.api.test_recommendation_integration import candidate
from tests.integration.test_recommendation_chat_api_storage import (
    C01,
    R01,
    body,
    chat,
    counts,
    expire_context,
    following,
    interpretation_count,
    post,
    runtime,
    target,
)
from tests.integration.test_recommendation_execution_storage import WriteHook

pytestmark = pytest.mark.integration
R02 = "/api/v1/recommendations/retry-routes"


def retry_body(value, data):
    result = following(value, data)
    result.pop("conditions_patch", None)
    result.update({k: v for k, v in target(data).items() if k != "place_id"})
    return result


def ids(data):
    return [card["place"]["value"]["place_id"] for card in data["result"]["cards"]]


async def original(env, *, general=True):
    places = [candidate(str(i), latitude=37.57 + i / 1000) for i in range(3)]
    for item in places:
        item.collected_at = datetime.now(UTC) - timedelta(hours=1)
        item.place.address = "원래 주소"
    env.source.search.side_effect = None
    env.source.search.return_value = places, True
    if general:
        env.source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "경로 조회 실패")
    value = body()
    value["current_conditions"]["max_travel_seconds"] = 600
    data = (await post(env, R01, value)).json()["data"]
    assert data["kind"] == ("general_candidates" if general else "recommendation")
    # Any accidental reuse of the live provider objects would corrupt the retry.
    for item in places:
        item.place.coordinates.latitude = 38
        item.place.name = "변경된 장소"
        item.collected_at = datetime.now(UTC)
    for provider in (env.source, env.model, env.kakao):
        provider.reset_mock()
    env.source.route.side_effect = None
    return value, data


async def stored_rows(env, data):
    result_id = UUID(data["result"]["result_id"])
    async with env.app.state.session_factory() as session:
        return [
            (await session.execute(select(model.__table__).where(model.result_id == result_id)))
            .mappings()
            .all()
            for model in (RecommendationResult, RecommendationCard)
        ]


def assert_only_retry(env, calls):
    assert env.source.route.await_count == calls
    env.source.search.assert_not_awaited()
    env.source.region.assert_not_awaited()
    assert interpretation_count(env) == 0
    assert not env.kakao.mock_calls


@pytest.mark.parametrize(
    "case,kind,stage,routes,result_count",
    [
        ("partial", "recommendation", "partial", 3, 2),
        ("failed", "general_candidates", "failed", 3, 2),
        ("time_limit", "no_candidates", "ok", 3, 1),
        ("excluded", "no_candidates", "skipped", 0, 1),
        ("mixed_time_failure", "no_candidates", "partial", 3, 1),
    ],
)
def test_retry_outcomes_keep_original_candidates_and_conditions(
    normalized_database, case, kind, stage, routes, result_count
):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            before = await stored_rows(env, first)
            request = retry_body(value, first)
            failure = ServiceError("PROVIDER_TIMEOUT", "경로 조회 실패")
            if case == "partial":
                env.source.route.side_effect = [(300, 100), failure, (600, 200)]
            elif case == "failed":
                env.source.route.side_effect = failure
            elif case == "time_limit":
                env.source.route.return_value = (601, 100)
            elif case == "excluded":
                request["excluded_place_ids"] = ids(first)
            else:
                env.source.route.side_effect = [(601, 100), failure, (602, 200)]
            response = await post(env, R02, request)
            data = response.json()["data"]
            assert data["kind"] == kind and data["stages"]["routes"] == stage
            assert data["condition_update"] == first["condition_update"]
            assert data["stages"]["interpretation"] == "skipped"
            assert_only_retry(env, routes)
            for call, card in zip(
                env.source.route.await_args_list, first["result"]["cards"], strict=routes > 0
            ):
                assert (
                    call.args[0].model_dump()
                    == value["current_conditions"]["origin"]["coordinates"]
                )
                assert call.args[1].model_dump() == card["place"]["value"]["coordinates"]
            if data["result"]:
                old = first["result"]
                assert data["result"]["result_id"] != old["result_id"]
                assert data["result"]["context_token"] != old["context_token"]
                originals = {c["place"]["value"]["place_id"]: c["place"] for c in old["cards"]}
                for card in data["result"]["cards"]:
                    assert card["place"] == originals[card["place"]["value"]["place_id"]]
                new_rows = await stored_rows(env, data)
                assert new_rows[0][0]["source_result_id"] == UUID(old["result_id"])
                if case == "partial":
                    assert ids(data) == ["kakao:0", "kakao:2"]
                    env.interpretation = Interpretation(intent="recommendation_reason")
                    reason = (await post(env, C01, chat(value, target=target(data)))).json()["data"]
                    assert reason["answer"]["original_result_id"] == data["result"]["result_id"]
                    assert any(
                        i["value"] == {"duration_seconds": 300, "distance_meters": 100}
                        for i in reason["answer"]["information"]
                    )
            else:
                assert data["result"] is None
            assert await stored_rows(env, first) == before
            assert await counts(env) == (1, 1, result_count)
            calls = list(env.source.mock_calls), list(env.model.mock_calls)
            replay = await post(env, R02, request)
            assert replay.json()["data"] == data
            assert (env.source.mock_calls, env.model.mock_calls) == calls
            assert await counts(env) == (1, 1, result_count)

    asyncio.run(run())


def test_concurrent_retry_is_computed_and_committed_once_and_replay_checks_context(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            request = retry_body(value, first)
            entered, release = asyncio.Event(), asyncio.Event()

            async def route(*args):
                entered.set()
                await release.wait()
                return 300, 100

            env.source.route.side_effect = route
            a = asyncio.create_task(post(env, R02, request))
            b = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                b = asyncio.create_task(post(env, R02, deepcopy(request)))
                assert await counts(env) == (1, 1, 1)
                release.set()
                responses = await asyncio.gather(a, b)
            finally:
                release.set()
                await asyncio.gather(*[t for t in (a, b) if t], return_exceptions=True)
            assert responses[0].json()["data"] == responses[1].json()["data"]
            assert_only_retry(env, 3)
            assert env.model.complete.await_count == 1
            assert await counts(env) == (1, 1, 2)
            altered = deepcopy(request)
            altered["excluded_place_ids"] = ["kakao:0"]
            conflict = await post(env, R02, altered, 409)
            assert conflict.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
            await expire_context(env, first)
            expired = await post(env, R02, request, 410)
            assert expired.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert_only_retry(env, 3)
            assert await counts(env) == (1, 1, 2)

    asyncio.run(run())


def test_retry_commit_failure_keeps_original_and_exclusions_and_replays_failure(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            before = await stored_rows(env, first)
            request = retry_body(value, first)
            request["excluded_place_ids"] = ["kakao:0"]
            writers = env.app.state.write_session_factory

            async def break_commit(session):
                await session.execute(update(ChatSession).values(condition_version=999))

            env.app.state.write_session_factory = WriteHook(writers, break_commit)
            response = await post(env, R02, request, 503)
            data = response.json()["data"]
            assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
            assert not data["condition_update"]["applied"] and data["result"] is None
            assert await stored_rows(env, first) == before
            assert await counts(env) == (1, 1, 1)
            entry = env.app.state.recommendation_progress.trip(UUID(value["trip_id"]))
            assert not entry.excluded
            assert (await post(env, R02, request, 503)).json()["data"] == data
            assert_only_retry(env, 2)
            env.app.state.write_session_factory = writers
            request["request_id"] = str(uuid4())
            recovered = (await post(env, R02, request)).json()["data"]
            assert ids(recovered) == ["kakao:1", "kakao:2"]
            assert_only_retry(env, 4)
            assert await counts(env) == (1, 1, 2)

    asyncio.run(run())


def test_retry_provider_failure_keeps_accepted_conditions_and_failure_metadata(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            request = retry_body(value, first)
            env.source.route.side_effect = ServiceError(
                "RATE_LIMITED", "호출 제한", retry_after_seconds=5
            )
            response = await post(env, R02, request, 429)
            data = response.json()["data"]
            assert data["condition_update"] == first["condition_update"]
            assert data["result"] is None and response.headers["retry-after"] == "5"
            replay = await post(env, R02, request, 429)
            assert replay.json()["data"] == data and replay.headers["retry-after"] == "5"
            assert_only_retry(env, 3)
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())


@pytest.mark.parametrize("provider_failure", [False, True])
def test_context_expiry_during_retry_prevents_commit(normalized_database, provider_failure):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            clock = [datetime.now(UTC)]

            async def reader():
                async with env.app.state.session_factory() as session:
                    yield RecommendationContextService(
                        RecommendationContextRepository(session), clock=lambda: clock[0]
                    )

            env.app.dependency_overrides[get_context_reader] = reader

            async def route(*args):
                clock[0] = datetime.fromisoformat(first["result"]["context_expires_at"])
                if provider_failure:
                    raise ServiceError("RATE_LIMITED", "호출 제한")
                return 300, 100

            env.source.route.side_effect = route
            failed = await post(env, R02, retry_body(value, first), 410)
            assert failed.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert not failed.json()["data"]["condition_update"]["applied"]
            assert await counts(env) == (1, 1, 1)
            entry = env.app.state.recommendation_progress.trip(UUID(value["trip_id"]))
            assert not entry.excluded

    asyncio.run(run())


def test_legacy_candidate_without_collection_time_does_not_invent_a_new_time(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            async with env.app.state.write_session_factory.begin() as session:
                await session.execute(
                    update(RecommendationCard)
                    .where(RecommendationCard.result_id == UUID(first["result"]["result_id"]))
                    .values(place_collected_at=None)
                )
            data = (await post(env, R02, retry_body(value, first))).json()["data"]
            assert all(card["place"]["collected_at"] is None for card in data["result"]["cards"])
            assert_only_retry(env, 3)

    asyncio.run(run())


def test_retry_exclusions_and_search_progress_are_shared_without_consuming_search_pages(
    normalized_database,
):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            request = retry_body(value, first)
            request["excluded_place_ids"] = ["kakao:0"]
            entry = env.app.state.recommendation_progress.trip(UUID(value["trip_id"]))
            search_before = deepcopy(entry.search)
            second = (await post(env, R02, request)).json()["data"]
            assert ids(second) == ["kakao:1", "kakao:2"]
            assert entry.search == search_before
            assert entry.excluded == {"kakao:0", "kakao:1", "kakao:2"}
            # A new request using the original result also excludes previous successes.
            empty = (await post(env, R02, retry_body(value, first))).json()["data"]
            assert empty["kind"] == "no_candidates"
            assert_only_retry(env, 2)
            assert await counts(env) == (1, 1, 2)

    asyncio.run(run())


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("token", 422, "INVALID_CONTEXT"),
        ("result", 422, "INVALID_CONTEXT"),
        ("malformed_result", 422, "INVALID_CONTEXT"),
        ("trip", 422, "INVALID_CONTEXT"),
        ("recommendation", 422, "INVALID_CONTEXT"),
        ("expired", 410, "CONTEXT_EXPIRED"),
        ("conditions", 409, "CONTEXT_CONDITIONS_MISMATCH"),
        ("version", 409, "CONTEXT_CONDITIONS_MISMATCH"),
        ("lost_state", 410, "CONTEXT_EXPIRED"),
    ],
)
def test_invalid_retry_is_rejected_before_any_provider_call(
    normalized_database, case, status, code
):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env, general=case != "recommendation")
            request = retry_body(value, first)
            if case == "token":
                request["context_token"] = "A" * 43
            elif case == "result":
                request["result_id"] = str(uuid4())
            elif case == "malformed_result":
                request["result_id"] = "invalid"
            elif case == "trip":
                request["trip_id"] = str(uuid4())
            elif case == "expired":
                await expire_context(env, first)
            elif case == "conditions":
                request["current_conditions"]["headcount"] = 3
            elif case == "version":
                request["condition_version"] = 1
            elif case == "lost_state":
                env.app.state.recommendation_progress = MemoryProgressStore()
            result = await post(env, R02, request, status)
            assert result.json()["error"]["code"] == code
            assert not result.json()["data"]["condition_update"]["applied"]
            assert not env.source.mock_calls and not env.model.mock_calls
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())


def test_retry_allows_reverted_conditions_but_rejects_an_old_current_version(normalized_database):
    async def run():
        async with runtime(normalized_database) as env:
            value, first = await original(env)
            env.source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "경로 조회 실패")
            edit = following(value, first)
            edit["conditions_patch"] = {"headcount": 3}
            changed = (await post(env, R01, edit)).json()["data"]
            edit = following(edit, changed)
            edit["conditions_patch"] = {"headcount": 2}
            reverted = (await post(env, R01, edit)).json()["data"]
            assert reverted["condition_update"]["version"] == 2
            assert (
                reverted["condition_update"]["conditions"]
                == first["condition_update"]["conditions"]
            )
            env.source.reset_mock()
            env.source.route.side_effect = None
            stale = await post(env, R02, retry_body(value, first), 409)
            assert stale.json()["error"]["code"] == "CONTEXT_CONDITIONS_MISMATCH"
            env.source.route.assert_not_awaited()
            request = retry_body(value, first)
            request["condition_version"] = 2
            data = (await post(env, R02, request)).json()["data"]
            assert data["condition_update"]["version"] == 2
            assert ids(data) == ids(first)
            assert_only_retry(env, 3)
            assert (await stored_rows(env, data))[0][0]["source_result_id"] == UUID(
                first["result"]["result_id"]
            )

    asyncio.run(run())
