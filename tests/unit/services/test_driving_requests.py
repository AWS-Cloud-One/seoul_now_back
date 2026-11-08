import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.kakao import KakaoError, KakaoLocalClient
from app.core.exceptions import ServiceError
from app.schemas.location import Coordinates
from app.schemas.query_services import DrivingData
from app.services.driving_requests import DrivingRequests

NOW = datetime.now(UTC)
POINT = Coordinates(latitude=37.57, longitude=126.98)


def data(at=NOW):
    return DrivingData(
        origin=POINT,
        destination=POINT,
        checked_at=at,
        route={
            "value": {
                "duration_seconds": 600,
                "distance_meters": 1234,
                "geometry": None,
                "geometry_reason": "NOT_REQUESTED",
                "taxi_fare_krw": None,
                "toll_fare_krw": None,
            },
            "status": "available",
            "freshness": "fresh",
            "source": "kakao_mobility",
            "observed_at": None,
            "collected_at": at,
            "reason_code": None,
        },
    )


def store(seconds, **kwargs):
    return DrivingRequests(
        interval=0,
        clock=lambda: seconds[0],
        wall_clock=lambda: NOW + timedelta(seconds=seconds[0]),
        **kwargs,
    )


def test_receipt_ttl_boundary_immutable_copy_and_lru():
    async def run():
        seconds = [0]
        cache = store(seconds, capacity=1)
        fetch = AsyncMock(side_effect=lambda: data(NOW + timedelta(seconds=seconds[0])))
        first = await cache.get("route", fetch)
        first.route.value.duration_seconds = 1
        seconds[0] = 59.999
        again = await cache.get("route", fetch)
        assert again.route.value.duration_seconds == 600 and again.route.collected_at == NOW
        assert again.checked_at > NOW and fetch.await_count == 1
        seconds[0] = 60
        assert (await cache.get("route", fetch)).route.collected_at == NOW + timedelta(seconds=60)
        assert fetch.await_count == 2
        await cache.get("another", fetch)
        assert len(cache.entries) == 1 and "route" not in cache.entries

    asyncio.run(run())


def test_saved_receipt_is_not_extended_and_future_or_unknown_times_are_not_cached():
    async def run():
        seconds = [59]
        cache = store(seconds)
        cache.remember("route", data())
        fetch = AsyncMock(return_value=data(NOW + timedelta(seconds=60)))
        assert (await cache.get("route", fetch)).route.collected_at == NOW
        cache.remember("route", data())
        seconds[0] = 60
        await cache.get("route", fetch)
        fetch.assert_awaited_once()
        cache.remember("future", data(NOW + timedelta(seconds=61)))
        missing = data()
        missing.route.collected_at = None
        cache.remember("missing", missing)
        assert "future" not in cache.entries and "missing" not in cache.entries

    asyncio.run(run())


def test_parallel_duplicates_full_summary_reuse_and_cancelled_waiter():
    async def run():
        cache = store([0])
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch():
            started.set()
            await release.wait()
            return data()

        provider = AsyncMock(side_effect=fetch)
        owner = asyncio.create_task(cache.get("full", provider))
        await started.wait()
        waiter = asyncio.create_task(cache.get("summary", provider, aliases=("full",)))
        duplicate = asyncio.create_task(cache.get("summary", provider, aliases=("full",)))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        one, two = await asyncio.gather(owner, duplicate)
        assert one == two and one is not two
        await cache.get("summary", provider, aliases=("full",))
        provider.assert_awaited_once()

    asyncio.run(run())


def test_failed_singleflight_budget_and_rolling_window_count_attempts():
    async def run():
        seconds = [0]
        cache = store(seconds, budget=2)
        failure = AsyncMock(side_effect=ServiceError("PROVIDER_TIMEOUT", "timeout"))
        for _ in range(2):
            with pytest.raises(ServiceError) as error:
                await cache.get("route", failure)
            assert error.value.code == "PROVIDER_TIMEOUT"
        with pytest.raises(ServiceError) as error:
            await cache.get("other", failure)
        assert error.value.code == "RATE_LIMITED" and error.value.retry_after_seconds == 86400
        assert failure.await_count == 2 and not cache.entries
        seconds[0] = 86400
        good = AsyncMock(return_value=data(NOW + timedelta(seconds=86400)))
        await cache.get("other", good)
        good.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("retry_after", [None, 8, 86400])
def test_quota_cooldown_blocks_other_routes_but_allows_fresh_cache(retry_after):
    async def run():
        seconds = [0]
        cache = store(seconds)
        cache.remember("cached", data())
        blocked = AsyncMock(
            side_effect=ServiceError("RATE_LIMITED", "quota", retry_after_seconds=retry_after)
        )
        with pytest.raises(ServiceError):
            await cache.get("one", blocked)
        assert (await cache.get("cached", blocked)).route.value.duration_seconds == 600
        with pytest.raises(ServiceError) as error:
            await cache.get("two", blocked)
        assert error.value.retry_after_seconds == (retry_after or 60)
        blocked.assert_awaited_once()
        seconds[0] = retry_after or 60
        good = AsyncMock(return_value=data(NOW + timedelta(seconds=seconds[0])))
        await cache.get("two", good)
        good.assert_awaited_once()

    asyncio.run(run())


def test_global_concurrency_and_start_spacing_across_distinct_routes():
    async def run():
        active, peak, starts = 0, 0, []
        cache = DrivingRequests(concurrency=2, interval=0.01)

        async def fetch():
            nonlocal active, peak
            starts.append(asyncio.get_running_loop().time())
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return data(datetime.now(UTC))

        await asyncio.gather(*(cache.get(str(i), fetch) for i in range(5)))
        assert peak == 2
        assert all(b - a >= 0.009 for a, b in zip(starts, starts[1:], strict=False))

    asyncio.run(run())


def test_documented_400_minus_10_quota_is_not_generic_provider_error():
    async def run():
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(400, json={"code": -10, "msg": "private quota body"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises(KakaoError) as error:
                await KakaoLocalClient(http, "test-key").directions(POINT, POINT)
            assert error.value.status == 429 and error.value.retry_after_seconds == 86400
            assert "private" not in error.value.message and len(calls) == 1

    asyncio.run(run())
