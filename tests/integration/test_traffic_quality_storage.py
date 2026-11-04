"""Last usable TOPIS response, value quality, ordering and immutable attempt evidence."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RawPayload, RoadTrafficLatest
from app.modules.data_pipeline.pipelines.topis import run_topis
from app.repositories.collection import CollectionRepository
from app.storage.payloads import configured_store
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration
BASE_TIME = datetime(2020, 1, 1, tzinfo=UTC)


async def seed(factory):
    async with factory.begin() as session:
        await save_snapshot(
            session,
            "topis_geometry:2025",
            {
                "features": [
                    {
                        "id": "r1",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [127.01, 37.5]],
                        },
                    }
                ]
            },
        )


async def write(factory, row, minute, **kwargs):
    async with factory.begin() as session:
        return await save_snapshot(
            session,
            "topis:r1",
            {"rows": [{"link_id": "r1", **row}]},
            received_at=BASE_TIME + timedelta(minutes=minute),
            **kwargs,
        )


def test_invalid_responses_preserve_good_data_partial_responses_do_not_mix_ages(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed(factory)
            good = await write(factory, {"prcs_spd": "12", "prcs_trv_time": "60"}, 1)
            async with factory() as session:
                first = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                processed = first.processed_at
            for row, reason in [
                ({"prcs_spd": "NaN", "prcs_trv_time": "-1"}, "invalid_traffic_values"),
                ({}, "missing_traffic_values"),
            ]:
                result = await write(factory, row, 2)
                assert result.status == "failed" and result.error_code == reason
                assert result.processing["accepted"] == 0 and result.processing["rejected"] == 1
                async with factory() as session:
                    current = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                    assert (
                        current.speed_kmh,
                        current.travel_seconds,
                        current.payload_id,
                        current.processed_at,
                    ) == (12, 60, good.payload_id, processed)
                    receipt = await session.get(RawPayload, good.payload_id)
                    assert receipt.received_at == BASE_TIME + timedelta(minutes=1)
                    attempt = await CollectionRepository(session).latest_run(
                        "topis_traffic", "link:r1"
                    )
                    assert attempt.id == result.run_id and attempt.error_code == reason
                    report = await session.scalar(
                        select(RawPayload).where(
                            RawPayload.run_id == result.run_id,
                            RawPayload.part_key == "normalization",
                        )
                    )
                    assert (
                        report.parent_payload_id == result.payload_id and report.received_at is None
                    )
                    saved = configured_store().load_json(report)
                    assert saved["source_time_status"] == "unknown" and not saved["updated"]
                    assert saved["field_issues"]
            for row, expected, issue in [
                ({"prcs_spd": "0", "prcs_trv_time": "NaN"}, (0, None), "INVALID_NUMBER"),
                ({"prcs_spd": "", "prcs_trv_time": "20"}, (None, 20), "MISSING_VALUE"),
            ]:
                result = await write(factory, row, 3)
                assert result.status == "partial" and result.error_code == "partial_traffic_values"
                assert result.processing["accepted"] == 1 and result.processing["rejected"] == 0
                assert issue in result.processing["field_issues"].values()
                async with factory() as session:
                    current = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                    assert (current.speed_kmh, current.travel_seconds) == expected
                    assert current.payload_id == result.payload_id and current.observed_at is None
                    assert current.reason_code == "PARTIAL_TRAFFIC_VALUES"
            complete = await write(factory, {"prcs_spd": "0", "prcs_trv_time": "0"}, 4)
            assert complete.status == "success" and complete.processing["value_status"] == "valid"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_first_response_creates_no_latest_row_and_mixed_links_are_rejected(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed(factory)
            invalid = await write(factory, {"prcs_spd": "NaN"}, 1)
            assert invalid.status == "failed"
            async with factory.begin() as session:
                result = await save_snapshot(
                    session,
                    "topis:r1",
                    {
                        "rows": [
                            {"link_id": "r1", "prcs_spd": "10"},
                            {"link_id": "r2", "prcs_spd": "20"},
                        ]
                    },
                )
                assert result.error_code == "invalid_link_response"
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(RoadTrafficLatest)) == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_guard_and_missing_receipt_report_no_write(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed(factory)
            good = await write(factory, {"prcs_spd": "0", "prcs_trv_time": "0"}, 2)
            old = await write(factory, {"prcs_spd": "90", "prcs_trv_time": "30"}, 1)
            assert old.status == "partial" and old.error_code == "traffic_time_guard"
            assert old.processing["value_status"] == "valid" and not old.processing["updated"]
            async with factory.begin() as session:
                unknown = await save_snapshot(
                    session,
                    "topis:r1",
                    {"rows": [{"link_id": "r1", "prcs_spd": "5"}]},
                    payload_kind="derived_snapshot",
                )
                assert unknown.error_code == "missing_traffic_receipt"
                current = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert current.payload_id == good.payload_id
                # Preserve the existing observation-order guard even for a legacy timed row.
                current.observed_at = BASE_TIME + timedelta(minutes=1)
            newer = await write(factory, {"prcs_spd": "99", "prcs_trv_time": "1"}, 3)
            assert newer.error_code == "traffic_time_guard"
            async with factory() as session:
                current = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert current.payload_id == good.payload_id and current.speed_kmh == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("reverse", [False, True])
def test_competing_cycles_always_leave_the_later_receipt(normalized_database, reverse):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed(factory)
            await write(factory, {"prcs_spd": "0", "prcs_trv_time": "0"}, 0)
            order = [2, 1] if reverse else [1, 2]
            results = await asyncio.gather(
                *(write(factory, {"prcs_spd": str(i), "prcs_trv_time": str(i)}, i) for i in order)
            )
            newest = results[order.index(2)]
            async with factory() as session:
                latest = await session.get(RoadTrafficLatest, ("topis_traffic", "r1"))
                assert latest.speed_kmh == 2 and latest.payload_id == newest.payload_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cycle_reports_partial_value_quality_without_calling_it_success(
    normalized_database, monkeypatch, stored_payload
):
    from app.modules.data_pipeline.pipelines import topis

    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await seed(factory)

            async def fetch(link):
                return {"rows": [{"link_id": link, "prcs_spd": "0", "prcs_trv_time": ""}]}

            result = await run_topis(engine, fetch, ["r1"])
            assert result["status"] == "partial" and result["counts"] == {
                "partial_traffic_values": 1
            }
            async with factory() as session:
                _, document = await stored_payload(session, "topis_traffic", "network")
                processing = document["entries"][0]["processing"]
                assert processing["updated"] and processing["value_status"] == "partial"
        finally:
            await engine.dispose()

    asyncio.run(run())
