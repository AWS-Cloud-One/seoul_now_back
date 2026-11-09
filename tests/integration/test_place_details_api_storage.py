"""P01/P03 use authenticated saved coordinates, without changing recommendation rows."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.dialects.postgresql import insert

from app.clients.kakao import KakaoError
from app.models import MapFeature, MapLayerVersion, ParkingFacility
from app.modules.recommendation.types import RouteEstimate
from app.repositories.recommendation_context import RecommendationContextRepository
from tests.integration.test_recommendation_chat_api_storage import (
    R01,
    body,
    counts,
    following,
    post,
    runtime,
)
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


def travel_request(original, data, *, version=0):
    card = data["result"]["cards"][0]
    return f"/api/v1/places/{card['place']['value']['place_id']}/travel-time", {
        "request_id": str(uuid4()),
        "trip_id": original["trip_id"],
        "condition_version": version,
        "result_id": data["result"]["result_id"],
        "context_token": data["result"]["context_token"],
    }


async def parking_rows(env):
    async with env.app.state.write_session_factory.begin() as session:
        await session.execute(
            insert(MapLayerVersion)
            .values(
                layer="parking",
                geometry_version="p01-test",
                is_ready=True,
                coverage="test",
            )
            .on_conflict_do_update(
                index_elements=["layer"],
                set_={
                    "geometry_version": "p01-test",
                    "is_ready": True,
                },
            )
        )
        for i in range(25):
            key = uuid4()
            point = f"SRID=4326;POINT({126.98 + i * 0.00001} 37.57)"
            session.add(ParkingFacility(parking_id=key, name=f"등록 주차장 {i}", location=point))
            await session.flush()
            session.add(
                MapFeature(
                    layer="parking",
                    feature_id=str(key),
                    parking_id=key,
                    geometry=point,
                    name=f"등록 주차장 {i}",
                    identity_status="unverified",
                )
            )


@pytest.mark.parametrize("cold", [False, True])
def test_detail_pagination_eta_reuse_and_historical_result_with_new_version(
    normalized_database, cold
):
    async def run():
        async with runtime(normalized_database) as env:
            env.app.state.settings.kakao_rest_api_key = SecretStr("test-key")
            env.kakao.directions.return_value = route_body()
            env.kakao.region.return_value = {
                "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
            }
            if cold:
                env.source.route.return_value = RouteEstimate(
                    600, 1000, datetime.now(UTC) - timedelta(seconds=61)
                )
            original = body()
            first = (await post(env, R01, original)).json()["data"]
            await parking_rows(env)
            url, req = travel_request(original, first)
            detail_url = url.removesuffix("/travel-time")
            detail = await env.client.get(
                detail_url, headers={"Result-Context": req["context_token"]}
            )
            assert detail.status_code == 200, detail.text
            assert detail.headers["cache-control"] == "no-store"
            assert detail.json()["data"]["place"] == first["result"]["cards"][0]["place"]
            parking = detail.json()["data"]["parking"]
            assert parking["total_count"] == 25 and len(parking["items"]) == 20
            env.kakao.region.assert_not_awaited()
            page = await env.client.get(
                "/api/v1/parking/nearby",
                params={
                    "latitude": 37.57,
                    "longitude": 126.98,
                    "limit": 20,
                    "cursor": parking["next_cursor"],
                },
            )
            assert page.status_code == 200, page.text
            assert len(page.json()["data"]["parking"]["items"]) == 5
            baseline = await counts(env)
            response = await post(env, url, req)
            value = response.json()["data"]
            assert response.json()["request_id"] == req["request_id"]
            assert value["route"]["observed_at"] is None
            assert env.kakao.directions.await_count == int(cold)
            assert (await post(env, url, req)).json()["data"] == value
            fresh = await post(env, url, req | {"request_id": str(uuid4())})
            assert fresh.json()["data"]["route"] == value["route"]
            assert env.kakao.directions.await_count == int(cold)
            assert await counts(env) == baseline
            # A newer UI version may still inspect the original result's route.
            change = following(original, first)
            change["conditions_patch"] = {
                "origin": original["current_conditions"]["origin"]
                | {
                    "coordinates": {"latitude": 37.55, "longitude": 127.0},
                }
            }
            newer = (await post(env, R01, change)).json()["data"]
            req2 = req | {
                "request_id": str(uuid4()),
                "condition_version": newer["condition_update"]["version"],
            }
            checked = (await post(env, url, req2)).json()["data"]
            assert (
                checked["result_id"] == req["result_id"] and checked["base_condition_version"] == 1
            )
            assert checked["original_recommended_at"] == first["result"]["created_at"]
            assert checked["route"] == value["route"]
            if cold:
                args = env.kakao.directions.call_args
                assert (
                    args.args[0].model_dump()
                    == original["current_conditions"]["origin"]["coordinates"]
                )
                assert args.kwargs == {"include_geometry": False}
            before = env.kakao.directions.await_count
            stale = await env.client.post(url, json=req | {"request_id": str(uuid4())})
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "CONTEXT_CONDITIONS_MISMATCH"
            invalid = await env.client.post(
                url.replace("kakao:0", "kakao:not-in-result"), json=req2
            )
            assert invalid.status_code == 422
            assert env.kakao.directions.await_count == before
            async with env.app.state.session_factory() as session:
                saved = await RecommendationContextRepository(session).get_by_token_digest(
                    hashlib.sha256(req["context_token"].encode()).hexdigest(),
                )
                assert saved.cards[0].route_collected_at == datetime.fromisoformat(
                    first["result"]["cards"][0]["route"]["collected_at"],
                )
                assert saved.cards[0].duration_seconds == 600
                assert saved.result.condition_version == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,expected,code", [(504, 504, "PROVIDER_TIMEOUT"), (429, 429, "RATE_LIMITED")]
)
def test_p03_failure_replay_expiry_and_no_mutations(normalized_database, status, expected, code):
    async def run():
        from app.core.exceptions import ServiceError
        from tests.integration.test_recommendation_chat_api_storage import expire_context

        async with runtime(normalized_database) as env:
            env.app.state.settings.kakao_rest_api_key = SecretStr("test-key")
            env.source.route.side_effect = ServiceError("PROVIDER_TIMEOUT", "timeout")
            original = body()
            general = (await post(env, R01, original)).json()["data"]
            assert general["kind"] == "general_candidates"
            url, req = travel_request(original, general)
            env.kakao.directions.side_effect = KakaoError(
                status, "safe failure", 10 if status == 429 else None
            )
            baseline = await counts(env)
            for _ in range(2):
                response = await env.client.post(url, json=req)
                assert response.status_code == expected, response.text
                assert response.json()["error"]["code"] == code
                if status == 429:
                    assert response.headers["retry-after"] == "10"
            env.kakao.directions.assert_awaited_once()
            assert await counts(env) == baseline
            await expire_context(env, general)
            expired = await env.client.post(url, json=req)
            assert (
                expired.status_code == 410 and expired.json()["error"]["code"] == "CONTEXT_EXPIRED"
            )
            env.kakao.directions.assert_awaited_once()

    asyncio.run(run())
