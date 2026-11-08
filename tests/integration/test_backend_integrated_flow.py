"""Full service wiring and PostGIS; only external HTTP and AI outputs are simulated."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.api.dependencies import (
    get_http_client,
    get_kakao_local_client,
    get_recommendation_source,
)
from app.modules.ai.interpretation import Interpretation
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from tests.integration.test_current_place_answers import seed_population
from tests.integration.test_place_details_api_storage import travel_request
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
from tests.integration.test_recommendation_geography import seed
from tests.integration.test_recommendation_retry_api_storage import (
    R02,
    ids,
    retry_body,
    stored_rows,
)
from tests.unit.recommendation.test_geography import sections
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


@asynccontextmanager
async def integrated_runtime(url):
    async with runtime(url) as env:
        # Keep the real adapters, geography, parking, caches, readers and persistence.
        env.app.dependency_overrides.pop(get_recommendation_source)
        env.app.dependency_overrides.pop(get_kakao_local_client)
        settings = env.app.state.settings
        settings.kakao_rest_api_key = SecretStr("test-key")
        settings.seoul_citydata_api_key = SecretStr("test-key")
        state = env.provider = SimpleNamespace(
            calls=[], mode="ok", seconds=600, grade="서행", candidates=12
        )
        async with env.app.state.write_session_factory.begin() as session:
            await seed(session)
            await seed_population(session)

        def respond(request):
            state.calls.append(request)
            if request.url.host == "dapi.kakao.com":
                if request.url.path.endswith("coord2regioncode.json"):
                    return httpx.Response(
                        200,
                        json={
                            "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
                        },
                    )
                assert request.url.path.endswith("keyword.json")
                return httpx.Response(
                    200,
                    json={
                        "documents": [
                            {
                                "id": str(i),
                                "place_name": f"카페 {i}",
                                "category_group_code": "CE7",
                                "x": "126.98",
                                "y": "37.57",
                            }
                            for i in range(state.candidates)
                        ],
                        "meta": {"is_end": True},
                    },
                )
            if request.url.host == "openapi.seoul.go.kr":
                name = request.url.path.rsplit("/", 1)[-1]
                code = f"POI{int(name.rsplit(' ', 1)[-1]):03}"
                return httpx.Response(
                    200,
                    json={
                        "RESULT": {"CODE": "INFO-000"},
                        "CITYDATA": {
                            "AREA_CD": code,
                            "AREA_NM": name,
                            **sections(
                                ("1000001401", state.grade),
                                ("1000000301", state.grade),
                                at=datetime.now(UTC).isoformat(),
                            ),
                        },
                    },
                )
            assert request.url.host == "apis-navi.kakaomobility.com"
            assert request.url.path == "/v1/directions"
            assert request.url.params["summary"] == "true"
            if state.mode == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if state.mode == "quota":
                return httpx.Response(429, headers={"Retry-After": "60"})
            return httpx.Response(
                200, json=route_body(summary={"duration": state.seconds, "distance": 1000})
            )

        async def http():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                yield client

        env.app.dependency_overrides[get_http_client] = http
        yield env
        env.source.assert_not_called()
        assert not env.source.mock_calls and not env.kakao.mock_calls


def provider_calls(env, suffix):
    return [r for r in env.provider.calls if r.url.path.endswith(suffix)]


def progress_state(env, request):
    entry = env.app.state.recommendation_progress.trip(UUID(request["trip_id"]))
    return deepcopy((entry.search, entry.excluded, entry.conditions, entry.version))


async def detail(env, selected, status=200):
    response = await env.client.get(
        f"/api/v1/places/{selected['place_id']}",
        headers={"Result-Context": selected["context_token"]},
    )
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_full_trip_preserves_history_through_details_current_facts_changes_and_retry(
    normalized_database, no_runtime_files
):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            assert first["kind"] == "recommendation"
            assert first["result"]["search_scope"]["fallback"] is False
            card = first["result"]["cards"][0]
            assert card["reason"]["evidence"][0]["information"]["value"] == {"grade": "서행"}
            assert card["parking"]["status"] == "available"
            saved_first = await stored_rows(env, first)
            before = progress_state(env, original)
            calls = len(env.provider.calls)
            assert (await post(env, R01, original)).json()["data"] == first
            assert len(env.provider.calls) == calls
            place = (await detail(env, target(first)))["data"]
            assert place["place"] == card["place"]
            assert place["parking"]["total_count"] == card["parking"]["total_count"]
            url, check = travel_request(original, first)
            direct = (await post(env, url, check)).json()["data"]
            assert direct["route"] == card["route"]
            assert len(provider_calls(env, "/v1/directions")) == 1

            for topic in ("travel_time", "parking", "population"):
                env.interpretation = Interpretation(intent=topic)
                question = chat(following(original, first), target=target(first))
                answer = (await post(env, C01, question)).json()["data"]
                assert answer["kind"] == "answer" and answer["answer"]["topic"] == topic
                assert not answer["condition_update"]["applied"] and answer["result"] is None
                if topic == "travel_time":
                    assert answer["answer"]["information"] == [direct["route"]]
                elif topic == "parking":
                    assert answer["answer"]["information"][0]["value"]["total_count"] > 0
                else:
                    assert (
                        answer["answer"]["information"][0]["value"]["population"]["min_people"]
                        == 100
                    )
                interpreted = interpretation_count(env)
                assert (await post(env, C01, question)).json()["data"] == answer
                assert interpretation_count(env) == interpreted
                assert len(env.provider.calls) == calls
            assert progress_state(env, original) == before
            assert await counts(env) == (1, 1, 1)

            # Leftovers reuse the same ETA but re-evaluate current road evidence.
            env.interpretation = Interpretation(intent="recommendation")
            env.provider.grade = "정체"
            additional = (await post(env, C01, chat(following(original, first)))).json()["data"]
            assert set(ids(first)).isdisjoint(ids(additional))
            assert len(provider_calls(env, "keyword.json")) == 1
            assert additional["result"]["cards"][0]["reason"]["evidence"][0]["information"][
                "value"
            ] == {"grade": "정체"}
            env.interpretation = Interpretation(intent="recommendation", headcount=4)
            changed = (
                await post(
                    env, C01, chat(following(original, additional), message="네 명으로 변경")
                )
            ).json()["data"]
            assert changed["condition_update"]["version"] == 1
            successful = set(ids(first) + ids(additional) + ids(changed))
            assert len(successful) == 9
            assert len(provider_calls(env, "/v1/directions")) == 1

            # A different origin must miss the old route cache; timeouts leave general cards.
            move = following(original, changed)
            move["conditions_patch"] = {
                "origin": original["current_conditions"]["origin"]
                | {"coordinates": {"latitude": 37.55, "longitude": 127.0}}
            }
            env.provider.mode = "timeout"
            general = (await post(env, R01, move)).json()["data"]
            assert general["kind"] == "general_candidates"
            assert general["condition_update"]["version"] == 2
            assert successful.isdisjoint(ids(general))
            assert all(
                c["rank"] is None and c["route"]["value"] is None
                for c in general["result"]["cards"]
            )
            assert progress_state(env, original)[1] == successful
            saved_general = await stored_rows(env, general)
            calls = len(env.provider.calls)

            # Historical facts use the first result while current conditions stay at version 2.
            env.interpretation = Interpretation(intent="recommendation_reason")
            question = chat(following(original, general), target=target(first))
            reason = (await post(env, C01, question)).json()["data"]
            assert "인원 2명" in reason["message"]
            assert reason["condition_update"]["version"] == 2
            assert reason["condition_update"]["conditions"]["headcount"] == 4
            assert reason["answer"]["information"] == [
                evidence["information"] for evidence in card["reason"]["evidence"]
            ]
            env.interpretation = Interpretation(intent="travel_time")
            current = (
                await post(env, C01, chat(following(original, general), target=target(first)))
            ).json()["data"]
            assert current["answer"]["information"] == [direct["route"]]
            assert len(env.provider.calls) == calls

            # R02 recovers only the original general candidates without keyword searches.
            env.provider.mode, env.provider.seconds = "ok", 900
            searches, interpreted = (
                len(provider_calls(env, "keyword.json")),
                interpretation_count(env),
            )
            retry = retry_body(original, general)
            recovered = (await post(env, R02, retry)).json()["data"]
            assert recovered["kind"] == "recommendation"
            assert set(ids(recovered)) == set(ids(general))
            assert all(
                c["route"]["value"]["duration_seconds"] == 900 for c in recovered["result"]["cards"]
            )
            assert recovered["condition_update"]["version"] == 2
            assert len(provider_calls(env, "keyword.json")) == searches
            assert interpretation_count(env) == interpreted
            assert provider_calls(env, "/v1/directions")[-1].url.params["origin"] == "127.0,37.55"
            calls = len(env.provider.calls)
            assert (await post(env, R02, retry)).json()["data"] == recovered
            assert len(env.provider.calls) == calls
            assert progress_state(env, original)[1] == successful | set(ids(recovered))
            assert await stored_rows(env, first) == saved_first
            assert await stored_rows(env, general) == saved_general
            assert await counts(env) == (1, 3, 5)
            async with env.app.state.session_factory() as session:
                conditions = RecommendationConditionsRepository(session)
                trip = UUID(original["trip_id"])
                assert (await conditions.get_current(trip)).version == 2
                assert [(await conditions.get_version(trip, i)).headcount for i in range(3)] == [
                    2,
                    4,
                    4,
                ]
                saved = await RecommendationContextRepository(session).get(
                    trip, UUID(recovered["result"]["result_id"])
                )
                assert saved.result.source_result_id == UUID(general["result"]["result_id"])

    asyncio.run(run())


def test_quota_and_expired_context_do_not_block_other_facts_or_change_saved_state(
    normalized_database, no_runtime_files
):
    async def run():
        async with integrated_runtime(normalized_database) as env:
            env.provider.mode, env.provider.candidates = "timeout", 3
            original = body()
            general = (await post(env, R01, original)).json()["data"]
            assert general["kind"] == "general_candidates"
            baseline, saved = progress_state(env, original), await stored_rows(env, general)
            env.provider.mode = "quota"
            env.interpretation = Interpretation(intent="travel_time")
            question = chat(following(original, general), target=target(general))
            limited = await post(env, C01, question, 429)
            assert limited.json()["error"]["code"] == "RATE_LIMITED"
            assert limited.headers["retry-after"] == "60"
            assert limited.json()["data"]["condition_update"]["applied"] is False
            calls, interpreted = len(env.provider.calls), interpretation_count(env)
            replay = await post(env, C01, question, 429)
            assert replay.json()["data"] == limited.json()["data"]
            assert interpretation_count(env) == interpreted
            url, check = travel_request(original, general)
            assert (await post(env, url, check, 429)).json()["error"]["code"] == "RATE_LIMITED"
            assert len(env.provider.calls) == calls
            conflict = await post(env, C01, question | {"message": "다른 질문"}, 409)
            assert conflict.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
            for topic in ("parking", "population"):
                env.interpretation = Interpretation(intent=topic)
                answer = (
                    await post(env, C01, chat(following(original, general), target=target(general)))
                ).json()["data"]
                assert answer["kind"] == "answer" and answer["answer"]["topic"] == topic
            assert (await detail(env, target(general)))["data"]["parking"]["status"] == "available"
            assert len(env.provider.calls) == calls
            assert progress_state(env, original) == baseline
            assert await stored_rows(env, general) == saved
            assert await counts(env) == (1, 1, 1)

            await expire_context(env, general)
            interpreted = interpretation_count(env)
            for response in [
                await post(env, C01, question, 410),
                await post(env, url, check, 410),
            ]:
                assert response.json()["error"]["code"] == "CONTEXT_EXPIRED"
            assert (await detail(env, target(general), 410))["error"]["code"] == "CONTEXT_EXPIRED"
            assert interpretation_count(env) == interpreted
            assert len(env.provider.calls) == calls
            assert await counts(env) == (1, 1, 1)

    asyncio.run(run())
