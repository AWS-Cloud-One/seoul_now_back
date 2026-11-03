import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, PopulationLatest, RawPayload
from app.modules.data_pipeline.pipelines import citydata
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.repositories.normalization import NormalizedWriter

pytestmark = pytest.mark.integration


def test_latest_history_update_and_normalization_failure_retains_raw(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        data = {
            "AREA_CD": "POI001",
            "AREA_NM": "테스트",
            "LIVE_PPLTN_STTS": [{"AREA_PPLTN_MIN": "0", "AREA_PPLTN_MAX": "10", "REPLACE_YN": "N"}],
        }

        async def fetch():
            return data

        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "citydata_place_boundaries",
                    {
                        "features": [
                            {
                                "id": "POI001",
                                "properties": {"name": "테스트"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [
                                            [127, 37.5],
                                            [127.1, 37.5],
                                            [127.1, 37.6],
                                            [127, 37.6],
                                            [127, 37.5],
                                        ]
                                    ],
                                },
                            }
                        ]
                    },
                )
            assert (await citydata.run_citydata(engine, fetch, "테스트"))["status"] == "success"
            data["LIVE_PPLTN_STTS"][0]["AREA_PPLTN_MAX"] = "20"
            assert (await citydata.run_citydata(engine, fetch, "테스트"))["status"] == "success"
            async with factory() as s:
                assert (await s.get(PopulationLatest, ("seoul_citydata", "POI001"))).min_people == 0
                assert (
                    await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                ).max_people == 20

            def fail(*a, **kw):
                raise RuntimeError("private failure")

            monkeypatch.setattr(citydata, "normalize_snapshot", fail)
            data["LIVE_PPLTN_STTS"][0]["AREA_PPLTN_MAX"] = "30"
            assert (await citydata.run_citydata(engine, fetch, "테스트"))["status"] == "failed"
            async with factory() as s:
                assert (await CityDataRepository(s).get("POI001")).data["LIVE_PPLTN_STTS"][0][
                    "AREA_PPLTN_MAX"
                ] == "20"
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .join(CollectionRun)
                        .where(CollectionRun.source == "seoul_citydata")
                    )
                    == 3
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parking_projection_rolls_back_all_writes_but_keeps_failed_run_and_inputs(
    normalized_database, monkeypatch
):
    from app.models import ParkingSourceRecord
    from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE
    from app.modules.data_pipeline.pipelines.parking import project_citydata_parking

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:

            async def collect(index, capacity):
                data = {
                    "AREA_CD": f"POI00{index}",
                    "AREA_NM": f"area{index}",
                    "PRK_STTS": [
                        {
                            "PRK_CD": str(index),
                            "PRK_NM": f"parking{index}",
                            "LAT": "37.5",
                            "LNG": "127",
                            "CPCTY": str(capacity),
                        }
                    ],
                }

                async def fetch():
                    return data

                assert (await citydata.run_citydata(engine, fetch, data["AREA_NM"]))[
                    "status"
                ] == "partial"

            for index in (1, 2):
                await collect(index, 10)
            assert (await project_citydata_parking(engine))["status"] == "success"
            for index in (1, 2):
                await collect(index, 20)

            original = NormalizedWriter.parking
            calls = 0

            def fail_after_write(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                result = original(self, *args, **kwargs)
                if calls == 2:
                    raise RuntimeError("private database error")
                return result

            monkeypatch.setattr(NormalizedWriter, "parking", fail_after_write)
            result = await project_citydata_parking(engine)
            assert calls == 2
            assert result["status"] == "failed" and result["accepted"] == 0
            assert result["error_code"] == "normalization_failed"
            async with factory() as session:
                records = list(await session.scalars(select(ParkingSourceRecord)))
                assert len(records) == 2 and all(r.registered_capacity == 10 for r in records)
                failed_run = await session.get(CollectionRun, UUID(result["run_id"]))
                assert failed_run.source == CITYDATA_PARKING_SOURCE
                assert failed_run.status == "failed" and failed_run.run_kind == "reprocess"
                payloads = list(
                    await session.scalars(
                        select(RawPayload).where(RawPayload.run_id == failed_run.id)
                    )
                )
                assert len(payloads) == 2
                assert all(p.parent_payload_id and p.received_at is None for p in payloads)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parking_projection_reuses_original_receipt_and_parent_payload(normalized_database):
    from app.models import ParkingSourceRecord
    from app.modules.data_pipeline.pipelines.parking import project_citydata_parking as project

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        data = {
            "AREA_CD": "POI001",
            "AREA_NM": "test",
            "PRK_STTS": [
                {"PRK_CD": "1", "PRK_NM": "주차장", "LAT": "37.5", "LNG": "127", "CPCTY": "0"}
            ],
        }

        async def fetch():
            return data

        try:
            # A missing polygon does not invalidate the original response for parking projection.
            assert (await citydata.run_citydata(engine, fetch, "test"))["status"] == "partial"
            assert (await project(engine))["status"] == "success"
            async with factory() as s:
                r = await s.scalar(select(ParkingSourceRecord))
                receipt = r.collected_at
                rid = r.source_record_id
                raw = await s.get(RawPayload, r.last_payload_id)
                original = await s.get(RawPayload, raw.parent_payload_id)
                assert raw.received_at is None and original.received_at == receipt
                assert r.registered_capacity == 0
            assert (await project(engine))["status"] == "success"
            async with factory() as s:
                r = await s.get(ParkingSourceRecord, rid)
                assert r.collected_at == receipt
            # A later area's subset must not deactivate previously projected parking.
            data["AREA_CD"], data["AREA_NM"] = "POI002", "other"
            data["PRK_STTS"][0].update(PRK_CD="2", PRK_NM="다른 주차장")
            assert (await citydata.run_citydata(engine, fetch, "other"))["status"] == "partial"
            assert (await project(engine))["status"] == "success"
            # Omission from a citydata response is not a complete parking inventory.
            data["AREA_CD"], data["AREA_NM"] = "POI001", "test"
            data["PRK_STTS"] = []
            assert (await citydata.run_citydata(engine, fetch, "test"))["status"] == "partial"
            assert (await project(engine))["status"] == "success"
            async with factory() as s:
                records = list(await s.scalars(select(ParkingSourceRecord)))
                assert {r.provider_record_id for r in records} == {"1", "2"}
                assert all(r.is_active for r in records)
        finally:
            await engine.dispose()

    asyncio.run(run())
