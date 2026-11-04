"""Independent sections, immutable source selection and explicit reuse in a disposable DB."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import ParkingSourceRecord, PopulationLatest, RawPayload
from app.modules.data_pipeline.collectors.citydata import CityDataDocument, collect_citydata
from app.modules.data_pipeline.pipelines import citydata
from app.modules.data_pipeline.pipelines.parking import (
    project_citydata_parking,
    run_municipal_parking,
    run_parking,
)
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration
STAMP = datetime(2026, 11, 4, 1, tzinfo=UTC)


def document():
    return {
        "RESULT": {"RESULT.CODE": "INFO-000"},
        "list_total_count": 1,
        "CITYDATA": {
            "AREA_CD": "POI001",
            "AREA_NM": "test",
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
            "PRK_STTS": [
                {
                    "PRK_CD": "1",
                    "PRK_NM": "parking",
                    "LAT": "37.5",
                    "LNG": "127",
                    "CPCTY": "10",
                    "CUR_PRK_YN": "Y",
                    "CUR_PRK_CNT": "0",
                    "CUR_PRK_TIME": "2026-11-04 09:00",
                }
            ],
            "ROAD_TRAFFIC_STTS": {
                "ROAD_TRAFFIC_STTS": [{"LINK_ID": "1", "XYLIST": "127_37.5|127.1_37.6"}]
            },
            "LIVE_YNA_NEWS": [{"article": "unused content"}],
            "UNEXPECTED": {"content": "unused"},
        },
    }


async def collect(engine, body, minute=0):
    async def fetch():
        return CityDataDocument(body, STAMP + timedelta(minutes=minute))

    return await citydata.run_citydata(engine, fetch, "test", expected_code="POI001")


async def boundary(factory):
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
                                [[126, 37], [128, 37], [128, 38], [126, 38], [126, 37]]
                            ],
                        },
                    }
                ]
            },
        )


def test_sections_survive_population_sql_failure_and_keep_original_evidence(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            body = document()
            first = await collect(engine, body)
            assert first["status"] == "success" and first["accepted"] == 3
            original_id = UUID(first["input_payload_id"])

            def fail(*args, **kwargs):
                raise RuntimeError("private database details")

            monkeypatch.setattr(citydata, "normalize_snapshot", fail)
            body["CITYDATA"]["PRK_STTS"][0]["CPCTY"] = "20"
            second = await collect(engine, body, 1)
            assert second["status"] == "partial" and second["accepted"] == 2
            assert second["sections"]["population"]["processing"] == "failed"
            assert second["sections"]["population"]["stored_observation"]["reused"]
            projected = await project_citydata_parking(
                engine, current_run_ids={"POI001": second["run_id"]}
            )
            assert projected["accepted"] == 1
            assert projected["section_inputs"][0]["current_cycle"] is True
            async with factory() as s:
                assert (
                    await s.get(PopulationLatest, ("seoul_citydata", "POI001"))
                ).payload_id == original_id
                record = await s.scalar(select(ParkingSourceRecord))
                assert record.registered_capacity == 20
                raw = await s.get(RawPayload, UUID(second["input_payload_id"]))
                selected = await s.get(RawPayload, UUID(second["selected_payload_id"]))
                assert configured_store().load_json(raw) == body
                chosen = configured_store().load_json(selected)
                assert "LIVE_YNA_NEWS" not in chosen["CITYDATA"]
                assert selected.parent_payload_id == raw.payload_id and selected.received_at is None
                assert raw.received_at == STAMP + timedelta(minutes=1)
                assert record.collected_at == raw.received_at
                # Consumers use the selected contract; they do not reopen the full original.
                repo = CityDataRepository(s)
                load = repo.store.load_json

                def selected_only(payload):
                    assert payload.payload_kind == "derived_snapshot"
                    return load(payload)

                monkeypatch.setattr(repo.store, "load_json", selected_only)
                road = (await repo.list_latest(section="road_geometry"))[0]
                assert road.payload_id == raw.payload_id and road.received_at == raw.received_at
                assert road.data["ROAD_TRAFFIC_STTS"]["ROAD_TRAFFIC_STTS"][0]["LINK_ID"] == "1"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("bad_section", ["missing", None, "", [{"PRK_CD": "1", "LAT": "NaN"}]])
def test_latest_bad_parking_section_reuses_old_input_with_explicit_state(
    normalized_database, bad_section
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            first = await collect(engine, document())
            assert (await project_citydata_parking(engine))["accepted"] == 1
            bad = document()
            if bad_section == "missing":
                del bad["CITYDATA"]["PRK_STTS"]
            else:
                bad["CITYDATA"]["PRK_STTS"] = bad_section
            last = await collect(engine, bad, 1)
            projected = await project_citydata_parking(
                engine, current_run_ids={"POI001": last["run_id"]}
            )
            assert projected["status"] == "partial" and projected["section_issues"] == 1
            evidence = projected["section_inputs"][0]
            assert evidence["input_payload_id"] == first["input_payload_id"]
            assert evidence["reused_previous"] and evidence["current_cycle"] is False
            assert evidence["received_at"] == STAMP.isoformat()
            assert evidence["latest_attempt"]["state"] == (
                "missing" if bad_section == "missing" else "invalid"
            )
            # Empty is a real current result: do not replace it with a previous nonempty list.
            empty = document()
            empty["CITYDATA"]["PRK_STTS"] = []
            await collect(engine, empty, 2)
            projected = await project_citydata_parking(engine)
            assert projected["status"] == "success" and projected["received"] == 0
            assert not projected["section_inputs"][0]["reused_previous"]
            async with factory() as s:
                record = await s.scalar(select(ParkingSourceRecord))
                assert record.is_active and record.collected_at == STAMP
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind,error",
    [
        ("error", "provider_result_error"),
        ("inner_only", "provider_invalid_response"),
        ("other_area", "area_identity_mismatch"),
    ],
)
def test_rejected_provider_envelope_is_saved_and_not_used_as_section_input(
    normalized_database, kind, error
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            first = await collect(engine, document())
            bad = document()
            if kind == "error":
                bad["RESULT"] = {"RESULT.CODE": "ERROR-200"}
            elif kind == "inner_only":
                bad = bad["CITYDATA"]
            else:
                bad["CITYDATA"]["AREA_CD"] = "POI002"
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json=bad))
            ) as http:
                result = await citydata.run_citydata(
                    engine,
                    lambda: collect_citydata(http, "test", "test", expected_code="POI001"),
                    "test",
                    expected_code="POI001",
                )
            assert result["status"] == "failed" and result["error_code"] == error
            assert result["selected_payload_id"] is None
            async with factory() as s:
                raw = await s.get(RawPayload, UUID(result["input_payload_id"]))
                assert configured_store().load_json(raw) == bad
                snapshot = (await CityDataRepository(s).list_latest(section="parking"))[0]
                assert str(snapshot.payload_id) == first["input_payload_id"]
                assert snapshot.evidence["latest_attempt"]["state"] == "failed"
                assert snapshot.evidence["reused_previous"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_municipal_coordinate_enrichment_is_derived_not_provider_response(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:

            async def registration():
                return [{"PKLT_CD": "1", "PKLT_NM": "p", "LAT": "37.5", "LOT": "127"}]

            async def municipal():
                return [{"PKLT_CD": "1", "PKLT_NM": "p", "TPKCT": "10"}]

            await run_parking(engine, registration, "seoul_getparkinfo")
            result = await run_municipal_parking(engine, municipal)
            assert result["accepted"] == 1
            async with factory() as s:
                enriched = await s.scalar(
                    select(RawPayload).where(RawPayload.run_id == UUID(result["run_id"]))
                )
                assert enriched.payload_kind == "derived_snapshot" and enriched.received_at is None
                original = await s.get(RawPayload, enriched.parent_payload_id)
                assert (
                    original.payload_kind == "provider_response"
                    and original.received_at is not None
                )
                assert "LAT" not in configured_store().load_json(original)["rows"][0]
                assert configured_store().load_json(enriched)[0]["LAT"] == 37.5
        finally:
            await engine.dispose()

    asyncio.run(run())
