import asyncio
from copy import deepcopy
from datetime import timedelta
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
)
from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE, SOURCE
from app.modules.data_pipeline.pipelines.citydata import run_citydata_batch
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.collection import CollectionRepository
from tests.fixtures.citydata import STAMP, boundary, document

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_response_archive")]


async def current_batch(engine, rows_by_area, *, minute=0):
    async def fetch(code, name):
        body = document(code, name)
        body["CITYDATA"]["PRK_STTS"] = rows_by_area[code]
        return CityDataDocument(body, STAMP + timedelta(minutes=minute))

    return (await run_citydata_batch(engine, fetch, {code: code for code in rows_by_area}))[
        "parking_storage"
    ]


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


def test_current_area_candidates_merge_and_do_not_refresh_old_observation(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        targets = {"POI001": "POI001", "POI002": "POI002"}
        registration = dict(
            document()["CITYDATA"]["PRK_STTS"][0],
            CUR_PRK_YN="N",
            CUR_PRK_CNT="",
            CUR_PRK_TIME="",
            RATES="1000",
            TIME_RATES="30",
            PAY_YN="Y",
        )
        live = dict(
            registration,
            CUR_PRK_YN="Y",
            CUR_PRK_CNT="0",
            CUR_PRK_TIME="2026-11-04 09:00",
            TIME_RATES=None,
        )
        try:
            await boundary(factory, targets)
            result = await current_batch(engine, {"POI001": [registration], "POI002": [live]})
            assert (result["accepted"], result["duplicates"], result["rejected"]) == (1, 1, 0)
            assert result["merge_summary"]["groups"] == 1
            async with factory() as session:
                record = await session.scalar(select(ParkingSourceRecord))
                rid, fid = record.source_record_id, record.parking_id
                assert record.base_minutes == 30 and record.base_fee_krw == 1000
                observation = await session.get(ParkingAvailabilityLatest, rid)
                assert observation.usable and observation.reported_available_spaces == 0
                assert observation.received_at == STAMP
            result = await current_batch(
                engine, {"POI001": [registration], "POI002": []}, minute=20
            )
            assert result["received"] == result["accepted"] == 1
            async with factory() as session:
                record = await session.get(ParkingSourceRecord, rid)
                assert record.parking_id == fid
                observation = await session.get(ParkingAvailabilityLatest, rid)
                assert (
                    observation.received_at == STAMP and observation.reported_available_spaces == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_current_conflicts_are_order_independent_and_preserve_last_good_values(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = dict(document()["CITYDATA"]["PRK_STTS"][0], RATES="1000", TIME_RATES="30")
        try:
            await boundary(factory, {"POI001": "POI001", "POI002": "POI002"})
            assert (await current_batch(engine, {"POI001": [row]}))["accepted"] == 1
            async with factory() as session:
                record = await session.scalar(select(ParkingSourceRecord))
                rid, fid = record.source_record_id, record.parking_id
            areas = {"POI001": [row], "POI002": [dict(row, RATES="2000")]}
            for ordered in (areas, dict(reversed(list(areas.items())))):
                result = await current_batch(engine, ordered, minute=1)
                assert (result["accepted"], result["rejected"], result["conflicts"]) == (0, 2, 2)
                assert result["rejection_reasons"] == {"conflicting_parking_registration": 2}
                async with factory() as session:
                    current = await session.get(ParkingSourceRecord, rid)
                    assert (current.parking_id, current.base_fee_krw, current.collected_at) == (
                        fid,
                        1000,
                        STAMP,
                    )
                    run_id = UUID(result["run_id"])
                    assert (await CollectionRepository(session).rejection_counts([run_id]))[
                        run_id
                    ] == result["rejection_reasons"]
                    assert (await session.get(CollectionRun, run_id)).status == "partial"
            result = await current_batch(
                engine, {"POI001": [row], "POI002": [dict(row, CUR_PRK_CNT="1")]}, minute=2
            )
            assert result["rejection_reasons"] == {"conflicting_parking_observation": 2}
            result = await current_batch(engine, {"POI001": [row], "POI002": [row]}, minute=3)
            assert (
                result["status"],
                result["received"],
                result["accepted"],
                result["duplicates"],
            ) == ("success", 2, 1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())
