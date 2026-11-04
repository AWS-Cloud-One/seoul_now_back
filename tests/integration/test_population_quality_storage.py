"""Direct population storage keeps quality, actual receipt, last good values and latest attempts."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    CollectionRejectionReason,
    CollectionRun,
    PopulationAttemptLatest,
    PopulationLatest,
)
from tests.fixtures.citydata import STAMP, boundary, collect, document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


def test_population_quality_partial_write_and_time_guard(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            data = document()
            assert (await collect(engine, data))["population_quality"]["updated"]
            row = data["CITYDATA"]["LIVE_PPLTN_STTS"][0]
            row.update(AREA_CONGEST_LVL="new", REPLACE_YN=None)
            partial = await collect(engine, data, 1)
            assert partial["status"] == "partial" and partial["population_quality"]["accepted"] == 1
            assert partial["population_quality"]["field_issues"] == {
                "congestion": "UNKNOWN_CONGESTION",
                "is_replacement": "MISSING_VALUE",
            }
            for change, error in [
                ({"AREA_PPLTN_MIN": "11"}, "invalid_or_missing_population_range"),
                (
                    {"AREA_PPLTN_MIN": "0", "PPLTN_TIME": "2026-11-03 09:00"},
                    "population_time_guard",
                ),
                ({"PPLTN_TIME": "2099-01-01 09:00"}, "future_population_time"),
                ({"PPLTN_TIME": None}, "population_time_guard"),
            ]:
                row.update(change)
                assert (await collect(engine, data, 2))["error_code"] == error
            async with factory.begin() as session:
                value = await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert (value.min_people, value.max_people) == (0, 10)
                assert value.received_at == STAMP + timedelta(minutes=1)
                assert value.is_replacement is None and value.congestion is None
                assert value.reason_code == "PARTIAL_POPULATION_VALUES"
                await session.execute(delete(CollectionRejectionReason))
                await session.execute(delete(CollectionRun))
            async with factory() as session:
                attempt = await session.get(PopulationAttemptLatest, "POI001")
                assert (
                    attempt.error_code == "population_time_guard"
                    and not attempt.observation_updated
                )
                assert await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unknown_observation_time_uses_only_real_receipt_and_late_delivery_cannot_overwrite(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            data = document()
            row = data["CITYDATA"]["LIVE_PPLTN_STTS"][0]
            row["PPLTN_TIME"] = None
            assert (await collect(engine, data, 2))["population_quality"]["updated"]
            row["AREA_PPLTN_MAX"] = "20"
            assert (await collect(engine, data, 1))["error_code"] == "population_time_guard"
            async with factory() as session:
                value = await session.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert value.observed_at is None and value.received_at == STAMP + timedelta(
                    minutes=2
                )
                assert value.max_people == 10 and value.reason_code == "SOURCE_TIME_UNKNOWN"
        finally:
            await engine.dispose()

    asyncio.run(run())
