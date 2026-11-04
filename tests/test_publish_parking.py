"""Real storage/publication checks replacing mocks tied to the retired JSON publisher."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import MapFeature, ParkingFacility, ParkingSourceRecord
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.parking_detail import ParkingDetailRepository
from app.services.map_publication import MapPublicationService
from app.services.parking_detail import ParkingDetailService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("partial", [True, False])
def test_partial_omission_keeps_ids_and_full_snapshot_deactivates_missing(
    normalized_database, partial
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        a = {
            "PKLT_CD": "1",
            "PKLT_NM": "기존 주차장",
            "LAT": 37.56,
            "LOT": 126.97,
            "TPKCT": "0",
            "PRK_CRG": "0",
        }
        b = {"PKLT_CD": "2", "PKLT_NM": "새 주차장", "LAT": 37.56, "LOT": 126.98}

        async def first():
            return [a, b]

        async def second():
            return [b, {**a, "LAT": 0}] if partial else [b]

        try:
            assert (await run_parking(engine, first, "seoul_getparkinfo"))["status"] == "success"
            async with factory.begin() as s:
                assert await MapPublicationService(s).publish("parking") == 2
                records = list(await s.scalars(select(ParkingSourceRecord)))
                identities = {
                    r.provider_record_id: (r.parking_id, r.source_record_id) for r in records
                }
                detail = await ParkingDetailService(ParkingDetailRepository(s)).get_detail(
                    identities["1"][0]
                )
                assert detail.records[0].registered.value.registered_capacity == 0
                assert detail.records[0].registered.value.fees.base_fee_krw == 0
                assert detail.availability.status == "unsupported"
            assert (await run_parking(engine, second, "seoul_getparkinfo"))["status"] == (
                "partial" if partial else "success"
            )
            async with factory.begin() as s:
                assert await MapPublicationService(s).publish("parking") == (2 if partial else 1)
                records = list(await s.scalars(select(ParkingSourceRecord)))
                assert identities == {
                    r.provider_record_id: (r.parking_id, r.source_record_id) for r in records
                }
                a_record = next(r for r in records if r.provider_record_id == "1")
                assert a_record.is_active is partial
                assert (await s.get(ParkingFacility, identities["1"][0])).is_active is partial
                features = list(await s.scalars(select(MapFeature)))
                assert {f.feature_id for f in features} == (
                    {str(x[0]) for x in identities.values()}
                    if partial
                    else {str(identities["2"][0])}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_no_geometry_does_not_publish_empty_ready_layer(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with async_sessionmaker(engine).begin() as s:
                with pytest.raises(RuntimeError, match="No normalized geometry"):
                    await MapPublicationService(s).publish("parking")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_renamed_source_keeps_uuid_and_verified_representative(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        row = {"PKLT_CD": "1", "PKLT_NM": "A", "LAT": 37.56, "LOT": 126.97}

        async def fetch():
            return [row]

        try:
            await run_parking(engine, fetch, "seoul_getparkinfo")
            async with factory.begin() as s:
                f = await s.scalar(select(ParkingFacility))
                f.identity_status = "verified"
                f.name = "수동 확인 명칭"
                original = f.parking_id
            await run_parking(engine, fetch, "seoul_getparkinfo")
            row["PKLT_NM"] = "B"
            result = await run_parking(engine, fetch, "seoul_getparkinfo")
            assert result["status"] == "success" and result["accepted"] == 1
            async with factory() as s:
                facilities = list(await s.scalars(select(ParkingFacility)))
                assert (
                    len(facilities) == 1
                    and facilities[0].parking_id == original
                    and facilities[0].name == "수동 확인 명칭"
                )
                assert (await s.scalar(select(ParkingSourceRecord))).name == "B"
        finally:
            await engine.dispose()

    asyncio.run(run())
