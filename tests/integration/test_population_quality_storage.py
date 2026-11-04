"""Population field quality survives storage and rejected input cannot erase valid counts."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import PopulationLatest, RawPayload
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration


def test_population_quality_partial_write_and_time_guard(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        receipt = datetime(2026, 11, 4, 1, tzinfo=UTC)
        data = {
            "AREA_CD": "POI001",
            "AREA_NM": "test",
            "PRK_STTS": [],
            "ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": []},
            "LIVE_PPLTN_STTS": [
                {
                    "AREA_CD": "POI001",
                    "AREA_NM": "test",
                    "AREA_PPLTN_MIN": "0",
                    "AREA_PPLTN_MAX": "10",
                    "AREA_CONGEST_LVL": "여유",
                    "REPLACE_YN": "N",
                    "PPLTN_TIME": "2026-11-04 09:00",
                }
            ],
        }

        async def save(at):
            async with factory.begin() as s:
                return await save_snapshot(s, "seoul_citydata", data, received_at=at)

        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "citydata_place_boundaries",
                    {
                        "features": [
                            {
                                "id": "POI001",
                                "properties": {"name": "test"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [[127, 37], [128, 37], [128, 38], [127, 38], [127, 37]]
                                    ],
                                },
                            }
                        ]
                    },
                )
            baseline = await save(receipt)
            assert (
                baseline.status == "success"
                and baseline.processing["population_quality"]["updated"]
            )
            async with factory.begin() as s:
                # 0009 migrated originals remain readable, but the newer report is never a source.
                original = await s.get(RawPayload, baseline.payload_id)
                original.payload_kind = "legacy_snapshot"
            async with factory() as s:
                assert (await CityDataRepository(s).get("POI001")).payload_id == baseline.payload_id
            row = data["LIVE_PPLTN_STTS"][0]
            row.update(AREA_CONGEST_LVL="new", REPLACE_YN=None)
            partial = await save(receipt + timedelta(minutes=1))
            assert (
                partial.status == "partial"
                and partial.processing["population_quality"]["accepted"] == 1
            )
            assert partial.processing["population_quality"]["field_issues"] == {
                "congestion": "UNKNOWN_CONGESTION",
                "is_replacement": "MISSING_VALUE",
            }
            async with factory() as s:
                value = await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert value.min_people == 0 and value.max_people == 10
                assert value.is_replacement is None and value.congestion is None
                assert value.reason_code == "PARTIAL_POPULATION_VALUES"
                raw = await s.scalar(
                    select(RawPayload).where(
                        RawPayload.run_id == partial.run_id,
                        RawPayload.payload_kind == "processing_report",
                    )
                )
                assert raw.parent_payload_id == partial.payload_id
                assert (
                    configured_store().load_json(raw)["population_quality"]["field_issues"]
                    == partial.processing["population_quality"]["field_issues"]
                )
            row.update(AREA_PPLTN_MIN="11")
            rejected = await save(receipt + timedelta(minutes=2))
            assert rejected.error_code == "invalid_or_missing_population_range"
            row.update(AREA_PPLTN_MIN="0", PPLTN_TIME="2026-11-03 09:00")
            old = await save(receipt + timedelta(minutes=3))
            assert (
                old.error_code == "population_time_guard"
                and old.processing["population_quality"]["accepted"] == 0
            )
            row["PPLTN_TIME"] = "2099-01-01 09:00"
            future = await save(receipt + timedelta(minutes=4))
            assert future.error_code == "future_population_time"
            async with factory() as s:
                value = await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                assert value.payload_id == partial.payload_id
                assert value.min_people == 0 and value.max_people == 10
        finally:
            await engine.dispose()

    asyncio.run(run())
