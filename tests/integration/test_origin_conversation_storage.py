"""Origin dialogue over HTTP, real PostGIS and commit boundaries; external I/O is mocked."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import update

from app.main import create_app
from app.models import ChatSession
from app.modules.ai.interpretation import Interpretation
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.state_inspection import expire_origin_drafts, origin_draft, trip_state
from tests.integration.test_backend_integrated_flow import integrated_runtime, provider_calls
from tests.integration.test_recommendation_chat_api_storage import (
    C01,
    R01,
    body,
    chat,
    counts,
    following,
    interpretation_count,
    post,
    target,
)
from tests.integration.test_recommendation_execution_storage import WriteHook
from tests.unit.services.test_origin_search import poi

pytestmark = pytest.mark.integration


def setup_origins(env, *rows, name="시청역"):
    env.provider.origins[name] = list(rows) if rows else [poi(1), poi(2)]
    env.interpretation = Interpretation(intent="recommendation", origin_name=name)


def select_origin(base, question, index=0):
    return chat(base, message="여기서 출발") | {
        "origin_action": {
            "action": "select",
            "context_token": question["origin_context"]["context_token"],
            "candidate_id": question["choices"][index]["value"]["candidate_id"],
        }
    }


def assert_unapplied(data, value):
    assert data["condition_update"] == {
        "applied": False,
        "conditions": value["current_conditions"],
        "version": value["condition_version"],
        "changes": [],
    }
    assert data["result"] is None


async def current(env, value):
    async with env.app.state.session_factory() as session:
        return await RecommendationConditionsRepository(session).get_current(UUID(value["trip_id"]))


def test_confirm_select_commit_replay_and_historical_questions_with_real_storage(
    normalized_database,
):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            setup_origins(env)
            env.interpretation = Interpretation(
                intent="recommendation",
                origin_name="시청역",
                max_travel_seconds=1200,
                purposes=[{"purpose": "cafe"}],
                clear_fields=["headcount"],
            )
            original = body()
            original["current_conditions"].update(origin=None, purposes=[])
            query = chat(original, message="시청역에서 20분 이내 카페, 인원 제한은 해제")
            first = (await post(env, C01, query)).json()["data"]
            assert_unapplied(first, original)
            question = first["clarification"]
            assert question["origin_context"]["mode"] == "choose"
            assert question["pending_patch"]["headcount"] is None
            assert await counts(env) == (0, 0, 0)
            assert await current(env, original) is None
            assert not provider_calls(env, "/v1/directions")
            assert interpretation_count(env) == 1

            # A genuine commit gate proves neither the result nor the origin
            # transition is published while the DB transaction is still pending.
            entered, release = asyncio.Event(), asyncio.Event()
            writers = env.app.state.write_session_factory

            async def gate(session):
                entered.set()
                await release.wait()

            env.app.state.write_session_factory = WriteHook(writers, gate)
            chosen = select_origin(original, question, 1)
            echoed = deepcopy(question)
            echoed["choices"][1]["value"]["location"]["coordinates"]["latitude"] = 35.1
            echoed["pending_patch"]["max_travel_seconds"] = 9999
            chosen["pending_clarification"] = echoed
            a = asyncio.create_task(post(env, C01, chosen))
            b = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                b = asyncio.create_task(post(env, C01, deepcopy(chosen)))
                assert await counts(env) == (0, 0, 0)
                token = question["origin_context"]["context_token"]
                assert (await origin_draft(env.app, token)).used_by is None
                assert not a.done() and not b.done()
                release.set()
                one, two = await asyncio.wait_for(asyncio.gather(a, b), 5)
            finally:
                release.set()
                await asyncio.gather(*[task for task in (a, b) if task], return_exceptions=True)
                env.app.state.write_session_factory = writers
            selected = one.json()["data"]
            assert selected == two.json()["data"]
            assert selected["kind"] == "recommendation"
            assert selected["stages"]["interpretation"] == "skipped"
            accepted = selected["condition_update"]
            assert accepted["applied"] and accepted["version"] == 1
            assert accepted["conditions"]["origin"] == question["choices"][1]["value"]["location"]
            assert accepted["conditions"]["max_travel_seconds"] == 1200
            assert accepted["conditions"]["headcount"] is None
            assert await counts(env) == (1, 2, 1)
            saved = await current(env, original)
            assert (
                saved.version == 1
                and saved.conditions.model_dump(mode="json") == accepted["conditions"]
            )
            async with env.app.state.session_factory() as session:
                stored = await RecommendationContextService(
                    RecommendationContextRepository(session)
                ).resolve(selected["result"]["context_token"])
                assert stored.conditions == saved.conditions
                before = await RecommendationConditionsRepository(session).get_version(
                    UUID(original["trip_id"]), 0
                )
                assert before.origin is None and before.purposes == [] and before.headcount == 2
            assert interpretation_count(env) == 1
            assert len(provider_calls(env, "/v1/directions")) == 1
            calls = len(env.provider.calls), env.model.complete.await_count
            assert (await post(env, C01, chosen)).json()["data"] == selected
            assert calls == (len(env.provider.calls), env.model.complete.await_count)
            assert await counts(env) == (1, 2, 1)

            changed_body = following(original, selected)
            changed_body["conditions_patch"] = {"headcount": 4}
            changed = (await post(env, R01, changed_body)).json()["data"]
            assert changed["condition_update"]["version"] == 2
            stale = select_origin(following(original, changed), question)
            assert_unapplied((await post(env, C01, stale, 409)).json()["data"], stale)
            assert (await post(env, C01, chosen)).json()["data"] == selected
            assert await counts(env) == (1, 3, 2)
            env.interpretation = Interpretation(intent="recommendation_reason")
            reason = chat(following(original, changed), target=target(selected))
            answer = (await post(env, C01, reason)).json()["data"]
            assert_unapplied(answer, reason)
            assert answer["answer"]["original_result_id"] == selected["result"]["result_id"]
            assert answer["answer"]["information"] == [
                e["information"] for e in selected["result"]["cards"][0]["reason"]["evidence"]
            ]
            env.interpretation = Interpretation(intent="travel_time")
            travel = chat(following(original, changed), target=target(selected))
            answer = (await post(env, C01, travel)).json()["data"]
            assert_unapplied(answer, travel)
            assert answer["answer"]["topic"] == "travel_time" and answer["answer"]["checked_at"]
            assert await counts(env) == (1, 3, 2)

    asyncio.run(run())


def test_single_origin_then_missing_purpose_writes_only_after_all_answers(normalized_database):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            setup_origins(env, poi(1))
            original = body()
            original["current_conditions"].update(origin=None, purposes=[])
            first = (await post(env, C01, chat(original))).json()["data"]
            question = first["clarification"]
            assert question["origin_context"]["mode"] == "confirm"
            selected = (await post(env, C01, select_origin(original, question))).json()["data"]
            assert_unapplied(selected, original)
            waiting = selected["clarification"]
            assert waiting["origin_context"]["mode"] == "confirmed"
            assert waiting["required_fields"] == ["purposes"] and waiting["choices"] == []
            assert await counts(env) == (0, 0, 0)
            assert not provider_calls(env, "/v1/directions")
            env.interpretation = Interpretation(
                intent="recommendation", purposes=[{"purpose": "cafe"}]
            )
            answer = chat(original, message="카페") | {"pending_clarification": waiting}
            result = (await post(env, C01, answer)).json()["data"]
            assert result["kind"] == "recommendation" and await counts(env) == (1, 2, 1)
            assert (await current(env, original)).conditions.origin.model_dump(
                mode="json"
            ) == waiting["pending_patch"]["origin"]
            assert (
                len(
                    [
                        r
                        for r in provider_calls(env, "keyword.json")
                        if r.url.params["query"] == "시청역"
                    ]
                )
                == 1
            )

    asyncio.run(run())


def test_expired_and_outdated_questions_preserve_persisted_trip(normalized_database):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            original = body()
            saved = (await post(env, R01, original)).json()["data"]
            base = following(original, saved)
            setup_origins(env)
            query = chat(base, message="시청역에서 출발")
            question = (await post(env, C01, query)).json()["data"]["clarification"]
            await expire_origin_drafts(env.app)
            calls = len(env.provider.calls), env.model.complete.await_count
            expired = select_origin(base, question)
            assert_unapplied((await post(env, C01, expired, 410)).json()["data"], base)
            assert_unapplied((await post(env, C01, query, 410)).json()["data"], base)
            assert await counts(env) == (1, 1, 1)
            assert calls == (len(env.provider.calls), env.model.complete.await_count)
            assert (await current(env, base)).conditions.model_dump(mode="json") == base[
                "current_conditions"
            ]
            assert not (await trip_state(env.app, UUID(base["trip_id"]))).expired
            query["request_id"] = str(uuid4())
            fresh = (await post(env, C01, query)).json()["data"]["clarification"]
            changed = following(original, saved)
            changed["conditions_patch"] = {"headcount": 6}
            result = (await post(env, R01, changed)).json()["data"]
            calls = len(env.provider.calls), env.model.complete.await_count
            stale = select_origin(base, fresh)
            failure = await post(env, C01, stale, 409)
            assert failure.json()["error"]["code"] == "CONTEXT_CONDITIONS_MISMATCH"
            assert_unapplied(failure.json()["data"], base)
            assert await counts(env) == (1, 2, 2)
            assert (await current(env, base)).conditions.model_dump(mode="json") == result[
                "condition_update"
            ]["conditions"]
            assert calls == (len(env.provider.calls), env.model.complete.await_count)

    asyncio.run(run())


def test_rolled_back_selection_keeps_server_choice_for_new_request(normalized_database):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            setup_origins(env)
            original = body()
            question = (await post(env, C01, chat(original))).json()["data"]["clarification"]
            writers = env.app.state.write_session_factory

            async def break_commit(session):
                # Break a deferred FK after all condition/result writes to force
                # rollback at the actual PostgreSQL commit, not a mocked failure.
                await session.execute(update(ChatSession).values(condition_version=999))

            env.app.state.write_session_factory = WriteHook(writers, break_commit)
            chosen = select_origin(original, question)
            failed = await post(env, C01, chosen, 503)
            assert failed.json()["error"]["code"] == "DATA_UNAVAILABLE"
            assert_unapplied(failed.json()["data"], original)
            assert await counts(env) == (0, 0, 0)
            token = question["origin_context"]["context_token"]
            assert (await origin_draft(env.app, token)).used_by is None
            calls = len(env.provider.calls), env.model.complete.await_count
            assert (await post(env, C01, chosen, 503)).json()["data"] == failed.json()["data"]
            assert calls == (len(env.provider.calls), env.model.complete.await_count)
            env.app.state.write_session_factory = writers
            chosen["request_id"] = str(uuid4())
            recovered = (await post(env, C01, chosen)).json()["data"]
            assert recovered["kind"] == "recommendation" and await counts(env) == (1, 2, 1)
            assert (
                recovered["condition_update"]["conditions"]["origin"]
                == question["choices"][0]["value"]["location"]
            )
            assert (await origin_draft(env.app, token)).used_by == UUID(chosen["request_id"])

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["empty", "unsupported", "malformed", "provider_error"])
def test_unusable_origin_never_writes_conditions_or_runs_routes(normalized_database, failure):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            setup_origins(env, poi(1))
            if failure == "empty":
                env.provider.origins["시청역"] = []
            elif failure == "unsupported":
                env.provider.origins["시청역"] = [poi(1, address="부산 연제구")]
                env.provider.origin_regions[str(126.971)] = "부산광역시"
            elif failure == "malformed":
                env.provider.origins["시청역"] = [poi(1, y="invalid")]
            else:
                env.provider.origin_status = 500
            value = chat(body())
            provider_failure = failure in {"malformed", "provider_error"}
            response = await post(env, C01, value, 502 if provider_failure else 200)
            data = response.json()["data"]
            assert_unapplied(data, value)
            if not provider_failure:
                assert data["kind"] == "clarification"
                assert data["clarification"]["origin_context"]["mode"] == "refine"
                assert data["clarification"]["choices"] == []
                if failure == "unsupported":
                    assert "ORIGIN_OUT_OF_SERVICE_AREA" in [w["code"] for w in data["warnings"]]
            else:
                assert data["kind"] == "failure" and data["clarification"] is None
                assert response.json()["error"]["code"] == "PROVIDER_ERROR"
            assert await counts(env) == (0, 0, 0)
            assert not provider_calls(env, "/v1/directions")

    asyncio.run(run())


@asynccontextmanager
async def second_pod(env):
    """Another API process on the same database and the same external providers."""
    app = create_app(env.app.state.settings)
    app.dependency_overrides.update(env.app.dependency_overrides)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(**{**vars(env), "app": app, "client": client})


def test_conversation_moves_between_pods_mid_origin_question(normalized_database):
    """A rolling update sends the next turn to a different pod; nothing is lost."""

    async def run():
        async with integrated_runtime(normalized_database) as a, second_pod(a) as b:
            original = body()
            saved = (await post(a, R01, original)).json()["data"]
            base = following(original, saved)

            # Pod A asks which origin; pod B receives the answer.
            setup_origins(a)
            question = (await post(a, C01, chat(base, message="시청역에서 출발"))).json()["data"][
                "clarification"
            ]
            assert question["origin_context"]["mode"] == "choose"
            interpreted = interpretation_count(a)
            chosen = select_origin(base, question, 1)
            selected = (await post(b, C01, chosen)).json()["data"]
            assert selected["kind"] == "recommendation"
            assert selected["stages"]["interpretation"] == "skipped"
            accepted = selected["condition_update"]
            assert accepted["applied"] and accepted["version"] == 1
            assert accepted["conditions"]["origin"] == question["choices"][1]["value"]["location"]
            assert interpretation_count(a) == interpreted  # no AI call for the selection
            assert set(ids(saved)).isdisjoint(ids(selected))  # exclusions came along

            # The same request retried on pod A replays B's answer without any provider.
            calls = len(a.provider.calls), a.model.complete.await_count
            assert (await post(a, C01, deepcopy(chosen))).json()["data"] == selected
            assert calls == (len(a.provider.calls), a.model.complete.await_count)

            # The consumed question cannot be reused from either pod.
            token = question["origin_context"]["context_token"]
            assert (await origin_draft(b.app, token)).used_by == UUID(chosen["request_id"])
            reuse = select_origin(base, question, 0)
            assert_unapplied((await post(a, C01, reuse, 409)).json()["data"], reuse)
            state = await trip_state(a.app, original["trip_id"])
            assert state.version == 1 and not state.expired
            assert await counts(a) == (1, 2, 2)

    asyncio.run(run())


def ids(data):
    return [card["place"]["value"]["place_id"] for card in data["result"]["cards"]]
