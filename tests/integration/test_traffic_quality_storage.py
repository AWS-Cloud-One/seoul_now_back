"""Direct traffic values preserve zero, partial quality and actual receipt ordering."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RoadTrafficAttemptLatest, RoadTrafficLatest
from tests.fixtures.topis import response, seed_roads, write

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]
BASE_TIME = datetime(2026, 11, 4, tzinfo=UTC)


def test_invalid_values_keep_last_good_and_partial_values_do_not_mix_ages(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, ["r1"])
            assert (await write(factory, "r1", response("r1", 12, 60), BASE_TIME))[
                "status"
            ] == "success"
            async with factory() as s:
                processed = (await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))).processed_at
            for speed, travel, error in [
                ("NaN", "-1", "invalid_traffic_values"),
                ("", "", "missing_traffic_values"),
            ]:
                result = await write(
                    factory, "r1", response("r1", speed, travel), BASE_TIME + timedelta(minutes=1)
                )
                assert result["status"] == "failed" and result["error_code"] == error
                async with factory() as s:
                    value = await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                    assert (
                        value.speed_kmh,
                        value.travel_seconds,
                        value.processed_at,
                        value.received_at,
                    ) == (12, 60, processed, BASE_TIME)
                    assert (await s.get(RoadTrafficAttemptLatest, "r1")).error_code == error
            for speed, travel, expected in [("0", "NaN", (0, None)), ("", "20", (None, 20))]:
                result = await write(
                    factory, "r1", response("r1", speed, travel), BASE_TIME + timedelta(minutes=2)
                )
                assert result["status"] == "partial" and result["processing"]["updated"]
                async with factory() as s:
                    value = await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                    assert (value.speed_kmh, value.travel_seconds) == expected
                    assert (
                        value.reason_code == "PARTIAL_TRAFFIC_VALUES" and value.observed_at is None
                    )
            assert (
                await write(factory, "r1", response("r1", 0, 0), BASE_TIME + timedelta(minutes=3))
            )["status"] == "success"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "data,error",
    [
        ({"rows": [{"link_id": "r1", "prcs_spd": "NaN"}]}, "invalid_traffic_values"),
        ({"rows": [{"link_id": "r2", "prcs_spd": "10"}]}, "invalid_link_response"),
        ({"rows": [{"link_id": "r1"}, {"link_id": "r1"}]}, "invalid_link_response"),
    ],
)
def test_invalid_first_response_cannot_create_an_observation(normalized_database, data, error):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, ["r1"])
            assert (await write(factory, "r1", data, BASE_TIME))["error_code"] == error
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 0
                assert not (await s.get(RoadTrafficAttemptLatest, "r1")).observation_updated
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_receipt_old_receipt_and_unknown_time_cannot_replace_good_row(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, ["r1"])
            await write(factory, "r1", response("r1", 0, 0), BASE_TIME)
            old = await write(
                factory, "r1", response("r1", 90, 30), BASE_TIME - timedelta(minutes=1)
            )
            assert old["error_code"] == "traffic_time_guard"
            assert (await write(factory, "r1", response("r1"), None))[
                "error_code"
            ] == "missing_traffic_receipt"
            async with factory.begin() as s:
                value = await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert value.speed_kmh == 0 and value.received_at == BASE_TIME
                value.observed_at = BASE_TIME
            result = await write(
                factory, "r1", response("r1", 99, 1), BASE_TIME + timedelta(minutes=1)
            )
            assert result["error_code"] == "traffic_time_guard"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("reverse", [False, True])
def test_competing_writes_leave_latest_receipt_and_attempt(normalized_database, reverse):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed_roads(factory, ["r1"])
            order = [2, 1] if reverse else [1, 2]
            await asyncio.gather(
                *(
                    write(factory, "r1", response("r1", i, i), BASE_TIME + timedelta(minutes=i))
                    for i in order
                )
            )
            async with factory() as s:
                value = await s.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert value.speed_kmh == 2 and value.received_at == BASE_TIME + timedelta(
                    minutes=2
                )
                attempt = await s.get(RoadTrafficAttemptLatest, "r1")
                assert attempt.attempted_at == value.received_at and attempt.observation_updated
        finally:
            await engine.dispose()

    asyncio.run(run())
