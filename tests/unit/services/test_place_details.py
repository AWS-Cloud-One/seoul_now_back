import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.recommendation.cards import unavailable_parking
from app.schemas.location import Coordinates
from app.schemas.pending import TravelTimeRequest
from app.services.driving_requests import DrivingRequests
from app.services.place_details import PlaceDetailReader, TravelTimeReader
from app.services.place_queries import PlaceQueryService
from app.services.read_replay import ReadReplay
from tests.fixtures.recommendation_context import NOW
from tests.unit.services.test_driving_requests import data
from tests.unit.test_recommendation_context import issued


async def scenario(*, general=False):
    contexts, repo, original, registered, stored = await issued(general=general)
    stored = replace(stored, card_coordinates=(Coordinates(latitude=37.57, longitude=126.98),))
    repo.get_by_token_digest.return_value = stored
    seconds = [0]

    def clock():
        return NOW + timedelta(seconds=seconds[0])

    contexts.clock = clock
    queries = PlaceQueryService(
        AsyncMock(),
        driving_requests=DrivingRequests(
            interval=0,
            clock=lambda: seconds[0],
            wall_clock=clock,
        ),
    )
    queries._driving = AsyncMock(side_effect=lambda *a, **kw: data(clock()))
    conditions = AsyncMock()
    conditions.get_current.return_value = SimpleNamespace(version=0)
    travel = TravelTimeReader(contexts, conditions, queries, ReadReplay(), clock=clock)
    parking = AsyncMock()
    parking.search.return_value = SimpleNamespace(parking=unavailable_parking())
    detail = PlaceDetailReader(contexts, parking, clock=clock)
    req = TravelTimeRequest(
        request_id=uuid4(),
        trip_id=original.trip_id,
        condition_version=0,
        result_id=registered.result["result_id"],
        context_token=registered.result["context_token"],
    )
    return SimpleNamespace(
        contexts=contexts,
        repo=repo,
        stored=stored,
        seconds=seconds,
        queries=queries,
        conditions=conditions,
        travel=travel,
        parking=parking,
        detail=detail,
        req=req,
    )


def following(req):
    return req.model_copy(update={"request_id": uuid4()})


@pytest.mark.parametrize("general", [False, True])
def test_detail_keeps_original_place_and_only_reads_current_parking(general):
    async def run():
        env = await scenario(general=general)
        env.seconds[0] = 100
        result = await env.detail.detail("kakao:original", env.req.context_token)
        assert result.place.value.name == "당시 카페"
        assert result.place.collected_at == NOW and result.checked_at == NOW + timedelta(
            seconds=100
        )
        assert result.original_recommended_at == (None if general else NOW)
        env.parking.search.assert_awaited_once_with(
            env.stored.card_coordinates[0],
            limit=20,
            cursor=None,
            verified_seoul=True,
        )
        env.queries._driving.assert_not_awaited()
        assert env.repo.save.await_count == 1  # Only fixture creation, never a read.
        env.parking.search.side_effect = SQLAlchemyError("private database")
        partial = await env.detail.detail("kakao:original", env.req.context_token)
        assert partial.place == result.place and partial.parking.status == "error"

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode,code",
    [
        ("token", "INVALID_CONTEXT"),
        ("trip", "INVALID_CONTEXT"),
        ("result", "INVALID_CONTEXT"),
        ("place", "INVALID_CONTEXT"),
        ("version", "CONTEXT_CONDITIONS_MISMATCH"),
        ("expired", "CONTEXT_EXPIRED"),
    ],
)
def test_invalid_context_never_queries_routes_or_parking(mode, code):
    async def run():
        env = await scenario()
        place_id = "kakao:original"
        if mode == "token":
            env.req.context_token = "invalid"
        elif mode == "trip":
            env.req.trip_id = uuid4()
        elif mode == "result":
            env.req.result_id = str(uuid4())
        elif mode == "place":
            place_id = "kakao:other"
        elif mode == "version":
            env.req.condition_version = 1
        else:
            env.seconds[0] = 1800
        with pytest.raises(ServiceError) as error:
            await env.travel.check(place_id, env.req)
        assert error.value.code == code
        env.queries._driving.assert_not_awaited()
        env.parking.search.assert_not_awaited()

    asyncio.run(run())


def test_saved_eta_new_request_refresh_and_same_request_replay_keep_distinct_times():
    async def run():
        env = await scenario()
        first = await env.travel.check("kakao:original", env.req)
        env.queries._driving.assert_not_awaited()
        assert first.route.collected_at == NOW
        env.seconds[0] = 60
        assert await env.travel.check("kakao:original", env.req) == first
        req = following(env.req)
        refreshed = await env.travel.check("kakao:original", req)
        env.queries._driving.assert_awaited_once_with(
            env.stored.conditions.origin.coordinates,
            env.stored.card_coordinates[0],
            include_geometry=False,
        )
        assert refreshed.route.collected_at == NOW + timedelta(seconds=60)
        env.seconds[0] = 119
        cached = await env.travel.check("kakao:original", following(req))
        assert cached.route.collected_at == refreshed.route.collected_at
        assert cached.checked_at > refreshed.checked_at
        assert env.stored.cards[0].route_collected_at == NOW
        env.seconds[0] = 1800
        with pytest.raises(ServiceError) as error:
            await env.travel.check("kakao:original", req)
        assert error.value.code == "CONTEXT_EXPIRED"
        assert env.queries._driving.await_count == 1

    asyncio.run(run())


def test_old_result_uses_original_origin_with_current_ui_version_and_id_conflicts():
    async def run():
        env = await scenario(general=True)
        env.conditions.get_current.return_value = SimpleNamespace(version=2)
        env.req.condition_version = 2
        result = await env.travel.check("kakao:original", env.req)
        assert result.base_condition_version == 2 and result.original_recommended_at is None
        assert env.queries._driving.call_args.args[0] == env.stored.conditions.origin.coordinates
        env.conditions.get_current.return_value.version = env.req.condition_version = 3
        with pytest.raises(ServiceError) as error:
            await env.travel.check("kakao:original", env.req)
        assert error.value.code == "REQUEST_ID_CONFLICT"
        env.queries._driving.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("detail", [False, True])
def test_expiry_during_provider_wait_rejects_response(detail):
    async def run():
        env = await scenario(general=True)

        async def expire(*args, **kwargs):
            env.seconds[0] = 1800
            return SimpleNamespace(parking=unavailable_parking()) if detail else data(NOW)

        if detail:
            env.parking.search.side_effect = expire
        else:
            env.queries._driving.side_effect = expire
        with pytest.raises(ServiceError) as error:
            if detail:
                await env.detail.detail("kakao:original", env.req.context_token)
            else:
                await env.travel.check("kakao:original", env.req)
        assert error.value.code == "CONTEXT_EXPIRED" and env.repo.save.await_count == 1

    asyncio.run(run())


def test_concurrent_request_replay_and_errors_do_not_retry_automatically():
    async def run():
        env = await scenario(general=True)
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args, **kwargs):
            started.set()
            await release.wait()
            raise ServiceError("PROVIDER_TIMEOUT", "timeout")

        env.queries._driving.side_effect = fetch
        owner = asyncio.create_task(env.travel.check("kakao:original", env.req))
        await started.wait()
        duplicate = asyncio.create_task(env.travel.check("kakao:original", env.req))
        release.set()
        results = await asyncio.gather(owner, duplicate, return_exceptions=True)
        assert all(isinstance(r, ServiceError) and r.code == "PROVIDER_TIMEOUT" for r in results)
        with pytest.raises(ServiceError):
            await env.travel.check("kakao:original", env.req)
        env.queries._driving.assert_awaited_once()

    asyncio.run(run())
