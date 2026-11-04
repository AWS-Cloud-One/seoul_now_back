import asyncio
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, PopulationLatest, RawPayload
from app.modules.data_pipeline.pipelines import citydata
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "code,error", [("INFO-100", "provider_auth_error"), ("ERROR-337", "provider_quota_exceeded")]
)
def test_batch_stops_on_shared_provider_error_and_archives_response(
    normalized_database, code, error
):
    import httpx

    from app.modules.data_pipeline.collectors.citydata import collect_citydata
    from app.storage.payloads import configured_store

    async def run():
        engine = create_async_engine(normalized_database)
        requests = []
        body = {"RESULT": {"CODE": code, "MESSAGE": "provider failure"}}

        def handler(request):
            requests.append(True)
            return httpx.Response(200, json=body)

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

                async def fetch(area_code, name):
                    return await collect_citydata(client, "test", name, expected_code=area_code)

                result = await citydata.run_citydata_batch(
                    engine, fetch, {"POI001": "first", "POI002": "second", "POI003": "third"}
                )
            assert len(requests) == 1
            assert result["status"] == "paused_provider_limit"
            assert result["stop_reason"] == error
            assert result["completed"] == 1 and result["remaining"] == 2
            async with async_sessionmaker(engine)() as session:
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.source == "seoul_citydata")
                    )
                )
                assert len(runs) == 1 and runs[0].error_code == error
                original = await session.scalar(
                    select(RawPayload).where(
                        RawPayload.run_id == runs[0].id,
                        RawPayload.payload_kind == "provider_response",
                    )
                )
                assert configured_store().load_json(original) == body
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_citydata_keeps_last_observation_and_records_rejection(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        baseline = {
            "AREA_CD": "POI001",
            "AREA_NM": "테스트",
            "PRK_STTS": [],
            "ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": []},
            "LIVE_PPLTN_STTS": [
                {
                    "AREA_CD": "POI001",
                    "AREA_NM": "테스트",
                    "AREA_PPLTN_MIN": "0",
                    "AREA_PPLTN_MAX": "10",
                    "REPLACE_YN": "N",
                    "AREA_CONGEST_LVL": "여유",
                    "PPLTN_TIME": "2020-01-01 09:00",
                }
            ],
            "UNSELECTED_SECTION": {"unknown": "preserved"},
        }
        data = deepcopy(baseline)

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
            accepted = await citydata.run_citydata(engine, fetch, "테스트", expected_code="POI001")
            assert (accepted["status"], accepted["unit"], accepted["accepted"]) == (
                "success",
                "section",
                3,
            )
            assert accepted["scope"] == "citydata_section_processing"
            async with factory() as s:
                original = await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                original_payload, observed = original.payload_id, original.observed_at

            cases = [
                ("outer_code", "area_identity_mismatch", "failed"),
                ("outer_name", "area_identity_mismatch", "failed"),
                ("inner_code", "population_area_mismatch", "partial"),
                ("inner_name", "population_area_mismatch", "partial"),
                ("missing", "missing_population_section", "partial"),
                ("empty", "empty_population_section", "partial"),
                ("invalid_time", "invalid_population_time", "partial"),
                ("future", "future_population_time", "partial"),
                ("conflict", "conflicting_population_rows", "partial"),
            ]
            for case, reason, status in cases:
                data = deepcopy(baseline)
                row = data["LIVE_PPLTN_STTS"][0]
                row["AREA_PPLTN_MAX"] = "999"
                if case.startswith("outer_"):
                    data["AREA_CD" if case == "outer_code" else "AREA_NM"] = "other"
                elif case.startswith("inner_"):
                    row["AREA_CD" if case == "inner_code" else "AREA_NM"] = "other"
                elif case == "missing":
                    del data["LIVE_PPLTN_STTS"]
                elif case == "empty":
                    data["LIVE_PPLTN_STTS"] = []
                elif case in {"invalid_time", "future"}:
                    row["PPLTN_TIME"] = "bad" if case == "invalid_time" else "2099-01-01 09:00"
                else:
                    data["LIVE_PPLTN_STTS"].append(dict(row, AREA_PPLTN_MAX="998"))
                result = await citydata.run_citydata(
                    engine, fetch, "테스트", expected_code="POI001"
                )
                assert (result["status"], result["error_code"]) == (status, reason)
                assert (
                    result["received"],
                    result["accepted"],
                    result["rejected"],
                    result["duplicates"],
                ) == ((3, 0, 3, 0) if case.startswith("outer_") else (3, 2, 1, 0))
                if case == "conflict":
                    assert result["sections"]["population"]["conflicts"] == 1
                assert result["rejection_reasons"] == {
                    reason: 3 if case.startswith("outer_") else 1
                }
                async with factory() as s:
                    latest = await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                    assert (latest.min_people, latest.max_people) == (0, 10)
                    assert (latest.payload_id, latest.observed_at) == (
                        original_payload,
                        observed,
                    )
                    run_id = UUID(result["run_id"])
                    run_record = await s.get(CollectionRun, run_id)
                    assert (
                        run_record.rejected,
                        run_record.error_code,
                        run_record.completeness,
                    ) == (3 if case.startswith("outer_") else 1, reason, "unknown")
                    reasons = await CollectionRepository(s).rejection_counts([run_id])
                    assert reasons[run_id] == result["rejection_reasons"]
                    assert await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .where(RawPayload.run_id == run_id)
                    ) == (2 if case.startswith("outer_") else 3)
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(PopulationLatest)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_latest_history_update_and_normalization_failure_retains_raw(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        data = {
            "AREA_CD": "POI001",
            "AREA_NM": "테스트",
            "PRK_STTS": [],
            "ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": []},
            "LIVE_PPLTN_STTS": [
                {
                    "AREA_CD": "POI001",
                    "AREA_NM": "테스트",
                    "AREA_PPLTN_MIN": "0",
                    "AREA_PPLTN_MAX": "10",
                    "REPLACE_YN": "N",
                    "AREA_CONGEST_LVL": "여유",
                    "PPLTN_TIME": "2020-01-01 09:00",
                }
            ],
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
            assert (await citydata.run_citydata(engine, fetch, "테스트", expected_code="POI001"))[
                "status"
            ] == "success"
            data["LIVE_PPLTN_STTS"][0]["AREA_PPLTN_MAX"] = "20"
            assert (await citydata.run_citydata(engine, fetch, "테스트", expected_code="POI001"))[
                "status"
            ] == "success"
            async with factory() as s:
                assert (await s.get(PopulationLatest, ("seoul_citydata", "POI001"))).min_people == 0
                assert (
                    await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                ).max_people == 20

            def fail(*a, **kw):
                raise RuntimeError("private failure")

            monkeypatch.setattr(citydata, "normalize_snapshot", fail)
            data["LIVE_PPLTN_STTS"][0]["AREA_PPLTN_MAX"] = "30"
            assert (await citydata.run_citydata(engine, fetch, "테스트", expected_code="POI001"))[
                "status"
            ] == "partial"
            async with factory() as s:
                assert (await CityDataRepository(s).get("POI001")).data["LIVE_PPLTN_STTS"][0][
                    "AREA_PPLTN_MAX"
                ] == "30"
                assert (
                    await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                ).max_people == 20
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .join(CollectionRun)
                        .where(
                            CollectionRun.source == "seoul_citydata",
                            RawPayload.payload_kind == "provider_response",
                        )
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

                assert (
                    await citydata.run_citydata(
                        engine, fetch, data["AREA_NM"], expected_code=data["AREA_CD"]
                    )
                )["status"] == "partial"

            for index in (1, 2):
                await collect(index, 10)
            assert (await project_citydata_parking(engine))["status"] == "success"
            for index in (1, 2):
                await collect(index, 20)

            original = NormalizedWriter.upsert
            calls = 0

            def fail_after_write(self, name, *args, **kwargs):
                nonlocal calls
                result = original(self, name, *args, **kwargs)
                if name == "parking_source_records":
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("private database error")
                return result

            monkeypatch.setattr(NormalizedWriter, "upsert", fail_after_write)
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
                assert len(payloads) == 3
                derived = [p for p in payloads if p.payload_kind == "derived_snapshot"]
                assert len(derived) == 2
                assert all(p.parent_payload_id and p.received_at is None for p in derived)
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
            assert (await citydata.run_citydata(engine, fetch, "test", expected_code="POI001"))[
                "status"
            ] == "partial"
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
            assert (await citydata.run_citydata(engine, fetch, "other", expected_code="POI002"))[
                "status"
            ] == "partial"
            assert (await project(engine))["status"] == "success"
            # Omission from a citydata response is not a complete parking inventory.
            data["AREA_CD"], data["AREA_NM"] = "POI001", "test"
            data["PRK_STTS"] = []
            assert (await citydata.run_citydata(engine, fetch, "test", expected_code="POI001"))[
                "status"
            ] == "partial"
            assert (await project(engine))["status"] == "success"
            async with factory() as s:
                records = list(await s.scalars(select(ParkingSourceRecord)))
                assert {r.provider_record_id for r in records} == {"1", "2"}
                assert all(r.is_active for r in records)
        finally:
            await engine.dispose()

    asyncio.run(run())
