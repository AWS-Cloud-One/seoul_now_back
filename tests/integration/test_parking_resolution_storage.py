import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from geoalchemy2 import Geometry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    CollectionRun,
    ParkingAvailabilityLatest,
    ParkingFacility,
    ParkingSourceRecord,
    RawPayload,
)
from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
from app.modules.data_pipeline.pipelines.citydata import run_citydata
from app.modules.data_pipeline.pipelines.parking import project_citydata_parking, run_parking
from app.repositories.citydata import CityDataRepository
from app.repositories.collection import CollectionRepository
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration


def test_complementary_registration_and_live_storage_keep_both_input_chains(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        received = datetime(2026, 11, 4, 0, tzinfo=UTC)
        registration = {
            "PRK_CD": "1",
            "PRK_NM": "registration name",
            "ADDRESS": "lot address",
            "ROAD_ADDR": "",
            "LAT": 37.56,
            "LNG": 126.97,
            "CPCTY": "100",
            "CUR_PRK_YN": "N",
            "CUR_PRK_CNT": "",
            "CUR_PRK_TIME": "",
            "RATES": "1000",
            "TIME_RATES": "30",
            "PAY_YN": "Y",
        }
        live = {
            **registration,
            "PRK_NM": "live display alias",
            "ROAD_ADDR": "road address",
            "CUR_PRK_YN": "Y",
            "CUR_PRK_CNT": "0",
            "CUR_PRK_TIME": "2026-11-04 08:59",
            "TIME_RATES": None,
        }

        async def collect(code, rows, receipt):
            async def fetch():
                return CityDataDocument(
                    {"AREA_CD": code, "AREA_NM": code, "PRK_STTS": rows}, receipt, False
                )

            await run_citydata(engine, fetch, code, expected_code=code)

        try:
            await collect(
                "POI001",
                [registration, dict(registration, PRK_CD="2", CPCTY="34")],
                received + timedelta(minutes=10),
            )
            await collect("POI002", [live, dict(live, PRK_CD="2", CPCTY="119")], received)
            result = await project_citydata_parking(engine)
            assert (result["accepted"], result["duplicates"], result["rejected"]) == (1, 1, 2)
            assert result["rejection_reasons"] == {"conflicting_parking_registration": 2}
            assert result["merge_summary"]["groups"] == 1
            assert result["merge_summary"]["exact_duplicates"] == 0
            async with factory() as s:
                record = await s.scalar(select(ParkingSourceRecord))
                rid, fid = record.source_record_id, record.parking_id
                assert record.name == "registration name" and record.address == "road address"
                assert (
                    record.base_minutes == 30 and record.base_fee_krw == 1000 and record.base_usable
                )
                merged = await s.get(RawPayload, record.last_payload_id)
                selected = configured_store().load_json(merged)
                assert merged.payload_kind == "derived_snapshot" and merged.received_at is None
                assert selected["contract"] == "citydata-parking-merged-v1"
                for contributor in selected["inputs"]:
                    payload = await s.get(RawPayload, UUID(contributor["payload_id"]))
                    original_row = configured_store().load_json(payload)[contributor["row_index"]]
                    assert contributor["input_fields"] == original_row
                    assert contributor["input_sha256"] == fingerprint(original_row)
                    assert payload.parent_payload_id is not None
                observation = await s.get(ParkingAvailabilityLatest, rid)
                original_payload = observation.payload_id
                assert observation.usable and observation.reported_available_spaces == 0
                assert observation.received_at == received
                assert observation.payload_id != merged.payload_id
                donor = selected["inputs"][selected["observation_source"]]
                assert str(observation.payload_id) == donor["payload_id"]
                assert record.operation_payload_id == merged.payload_id
            # Fresh registration information must not make the old live observation look fresh.
            await collect(
                "POI001",
                [dict(registration, PRK_NM="renamed registration")],
                received + timedelta(minutes=20),
            )
            repeated = await project_citydata_parking(engine)
            assert repeated["merge_summary"]["groups"] == 1
            async with factory() as s:
                record = await s.get(ParkingSourceRecord, rid)
                observation = await s.get(ParkingAvailabilityLatest, rid)
                assert record.parking_id == fid and record.name == "renamed registration"
                assert (
                    observation.payload_id == original_payload
                    and observation.received_at == received
                )
                assert await s.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_other_sources_stay_separate_and_explicit_shared_facility_is_preserved(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = {
            "PKLT_CD": "1",
            "PKLT_NM": "original",
            "ADDR": "address",
            "LAT": 37.56,
            "LOT": 126.97,
        }

        async def fetch():
            return [row]

        try:
            for source in (SOURCE, MUNICIPAL_PARKING_SOURCE):
                assert (await run_parking(engine, fetch, source))["status"] == "success"
            async with factory.begin() as s:
                records = {r.source: r for r in await s.scalars(select(ParkingSourceRecord))}
                primary = records[SOURCE]
                linked = records[MUNICIPAL_PARKING_SOURCE]
                assert primary.parking_id != linked.parking_id
                fid, rid = primary.parking_id, primary.source_record_id
                linked.parking_id = (
                    fid  # Existing explicit correspondence, not a matching heuristic.
                )
            row["PKLT_NM"] = "source-specific name"
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 1
            async with factory() as s:
                assert (await s.get(ParkingFacility, fid)).name == "original"
                assert (await s.get(ParkingSourceRecord, rid)).name == "source-specific name"
                assert {r.parking_id for r in await s.scalars(select(ParkingSourceRecord))} == {fid}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_edits_keep_uuids_and_ambiguous_input_cannot_deactivate_or_overwrite(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = {
            "PKLT_CD": "1",
            "PKLT_NM": "original",
            "ADDR": "address",
            "LAT": 37.56,
            "LOT": 126.97,
        }
        second = dict(row, PKLT_CD="2", PKLT_NM="second")
        rows = [row, second]

        async def fetch():
            return deepcopy(rows)

        try:
            assert (await run_parking(engine, fetch, SOURCE))["status"] == "success"
            async with factory() as s:
                existing = list(
                    await s.scalars(
                        select(ParkingSourceRecord).order_by(ParkingSourceRecord.provider_record_id)
                    )
                )
                first_id, second_id = existing[0].source_record_id, existing[1].source_record_id
                facility_id, legacy_key = existing[0].parking_id, existing[0].legacy_record_id
            for change in ({"PKLT_NM": "renamed"}, {"ADDR": "corrected address"}, {"LAT": 37.57}):
                row.update(change)
                result = await run_parking(engine, fetch, SOURCE)
                assert result["status"] == "success" and result["accepted"] == 2
                async with factory() as s:
                    current = await s.get(ParkingSourceRecord, first_id)
                    facility = await s.get(ParkingFacility, facility_id)
                    assert (current.parking_id, current.legacy_record_id) == (
                        facility_id,
                        legacy_key,
                    )
                    assert (current.name, current.address) == (row["PKLT_NM"], row["ADDR"])
                    assert facility.name == row["PKLT_NM"]
                    assert (
                        await s.scalar(
                            select(func.ST_Y(ParkingSourceRecord.location.cast(Geometry))).where(
                                ParkingSourceRecord.source_record_id == first_id
                            )
                        )
                        == row["LAT"]
                    )
                    assert (
                        await s.scalar(
                            select(func.ST_Y(ParkingFacility.location.cast(Geometry))).where(
                                ParkingFacility.parking_id == facility_id
                            )
                        )
                        == row["LAT"]
                    )
                    assert await s.scalar(select(func.count()).select_from(ParkingFacility)) == 2
            # Two candidate positions both claim the original record by name/address.
            rows = [deepcopy(row), dict(row, LAT=37.58)]
            result = await run_parking(engine, fetch, SOURCE)
            assert result["status"] == "partial" and result["accepted"] == 0
            assert result["rejection_reasons"] == {"ambiguous_parking_identity": 2}
            async with factory() as s:
                assert (await s.get(ParkingSourceRecord, first_id)).is_active
                assert (await s.get(ParkingSourceRecord, second_id)).is_active
                assert (await s.get(ParkingFacility, facility_id)).is_active
                assert await s.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 2
            # Moving all available identifying facts gives no sound match, even if code is unique.
            rows = [dict(row, PKLT_NM="new", ADDR="new", LAT=37.6)]
            result = await run_parking(engine, fetch, SOURCE)
            assert result["rejection_reasons"] == {"identity_change_requires_review": 1}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_one_code_can_have_distinct_spaces_and_one_conflict_does_not_block_other_codes(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        first = {
            "PKLT_CD": "1",
            "PKLT_NM": "road section",
            "ADDR": "street",
            "LAT": 37.56,
            "LOT": 126.97,
            "PRK_CRG": "1000",
        }
        second = dict(first, LAT=37.57)
        independent = dict(first, PKLT_CD="2")
        rows = [second, first, independent]

        async def fetch():
            return rows

        try:
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 3
            async with factory() as s:
                old = list(await s.scalars(select(ParkingSourceRecord)))
                ids = {r.source_record_id for r in old}
                assert len({r.parking_id for r in old}) == 3
            first["PKLT_NM"] = "updated section"
            rows = [first, second, independent]
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 3
            rows = [
                dict(first, PRK_CRG="2000"),
                dict(first, PRK_CRG="3000"),
                dict(independent, PRK_CRG="4000"),
            ]
            for ordered in (rows, list(reversed(rows))):
                rows = ordered
                result = await run_parking(engine, fetch, SOURCE)
                assert (
                    result["status"],
                    result["accepted"],
                    result["rejected"],
                    result["conflicts"],
                ) == ("partial", 1, 2, 2)
                async with factory() as s:
                    current = list(await s.scalars(select(ParkingSourceRecord)))
                    assert {r.source_record_id for r in current} == ids
                    assert all(r.is_active for r in current)
                    assert all(
                        r.base_fee_krw == (1000 if r.provider_record_id == "1" else 4000)
                        for r in current
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_citydata_conflicts_keep_old_values_evidence_and_order_independence(
    normalized_database, monkeypatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = {
            "PRK_CD": "1",
            "PRK_NM": "parking",
            "ADDRESS": "address",
            "LAT": 37.56,
            "LNG": 126.97,
            "CPCTY": "100",
            "CUR_PRK_YN": "Y",
            "CUR_PRK_CNT": "10",
            "CUR_PRK_TIME": "2020-01-01 08:00",
            "RATES": "1000",
            "TIME_RATES": "30",
        }

        async def collect(code, value):
            async def fetch():
                return {"AREA_CD": code, "AREA_NM": code, "PRK_STTS": [value]}

            assert (await run_citydata(engine, fetch, code, expected_code=code))[
                "status"
            ] == "partial"

        try:
            await collect("POI001", row)
            assert (await project_citydata_parking(engine))["accepted"] == 1
            async with factory() as s:
                original = await s.scalar(select(ParkingSourceRecord))
                rid, fid, receipt, payload_id = (
                    original.source_record_id,
                    original.parking_id,
                    original.collected_at,
                    original.last_payload_id,
                )
            await collect("POI002", dict(row, RATES="2000"))
            normal_list = CityDataRepository.list_latest

            async def reverse(self, *args, **kwargs):
                return list(reversed(await normal_list(self, *args, **kwargs)))

            for reversed_order in (False, True):
                if reversed_order:
                    monkeypatch.setattr(CityDataRepository, "list_latest", reverse)
                result = await project_citydata_parking(engine)
                assert (
                    result["accepted"],
                    result["rejected"],
                    result["duplicates"],
                    result["conflicts"],
                ) == (0, 2, 0, 2)
                assert result["rejection_reasons"] == {"conflicting_parking_registration": 2}
                async with factory() as s:
                    stored = await s.get(ParkingSourceRecord, rid)
                    assert (
                        stored.parking_id,
                        stored.base_fee_krw,
                        stored.collected_at,
                        stored.last_payload_id,
                    ) == (fid, 1000, receipt, payload_id)
                    run_id = UUID(result["run_id"])
                    reasons = await CollectionRepository(s).rejection_counts([run_id])
                    assert reasons[run_id] == result["rejection_reasons"]
                    raw = list(
                        await s.scalars(select(RawPayload).where(RawPayload.run_id == run_id))
                    )
                    assert len(raw) == 3
                    derived = [p for p in raw if p.payload_kind == "derived_snapshot"]
                    assert len(derived) == 2 and all(p.parent_payload_id for p in derived)
                    assert {configured_store().load_json(p)[0]["RATES"] for p in derived} == {
                        "1000",
                        "2000",
                    }
                    assert (await s.get(CollectionRun, run_id)).status == "partial"
            # Equal registrations do not resolve different observations at the same time.
            await collect("POI002", dict(row, CUR_PRK_CNT="11"))
            result = await project_citydata_parking(engine)
            assert result["rejection_reasons"] == {"conflicting_parking_observation": 2}
            # Equal records fold to one with consistent counts; resolved facts can be used again.
            await collect("POI002", row)
            result = await project_citydata_parking(engine)
            assert (
                result["status"],
                result["received"],
                result["accepted"],
                result["duplicates"],
            ) == ("success", 2, 1, 1)
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(ParkingSourceRecord)) == 1
                assert (await s.get(ParkingSourceRecord, rid)).parking_id == fid
        finally:
            await engine.dispose()

    asyncio.run(run())
