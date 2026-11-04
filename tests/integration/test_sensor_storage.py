"""Actual PostGIS sensor persistence, provenance, retries, reference and clock guards."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import PedestrianSensor, PedestrianSensorLatest, RawPayload
from app.modules.data_pipeline.pipelines.sdot import save_sdot
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.sensors import SensorWriter
from app.storage.payloads import configured_store
from tests.fixtures.sdot import collected, location, observation, sensor_file

pytestmark = pytest.mark.integration
START = datetime(2026, 11, 4, 1, tzinfo=UTC)


async def boundary(factory):
    async with factory.begin() as s:
        await save_snapshot(
            s,
            "sgis_seoul_boundary",
            {
                "features": [
                    {
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [126.9, 37.4],
                                    [127.1, 37.4],
                                    [127.1, 37.7],
                                    [126.9, 37.7],
                                    [126.9, 37.4],
                                ]
                            ],
                        }
                    }
                ],
                "boundary_year": 2025,
            },
        )


async def save(factory, file, rows, step=0, *, collection=None):
    data = collection or collected(rows, (START + timedelta(minutes=step)).isoformat())
    async with factory.begin() as s:
        return await save_sdot(s, file, data, started_at=START + timedelta(minutes=step))


def test_locations_observations_and_original_evidence_are_separate(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        file = sensor_file([location(), location("2"), location("3", F="129")])
        try:
            await boundary(factory)
            result = await save(factory, file, [observation()])
            assert result["status"] == "success"
            assert result["records"] == 2 and result["storage"]["sensors_written"] == 2
            assert result["storage"]["observations_written"] == 1
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(PedestrianSensor)) == 2
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                empty = await s.get(PedestrianSensorLatest, ("sdot_people", "2"))
                assert latest.count_10min == 0 and latest.provider_sensor_id == "1"
                assert empty.count_10min is None and empty.attempt_status == "unobserved"
                sensor = await s.get(PedestrianSensor, ("sdot_people", "1"))
                original = await s.get(RawPayload, sensor.reference_payload_id)
                assert (
                    configured_store().read_bytes(
                        original.file_ref, original.sha256, original.byte_size
                    )
                    == file["body"]
                )
                page = await s.get(RawPayload, latest.payload_id)
                assert page.payload_kind == "provider_response" and page.received_at == START
                assert (
                    configured_store().load_json(page)["sDoTPeople"]["row"][0]["VISITOR_COUNT"]
                    == "0"
                )
                assert original.payload_kind == "source_file" and original.received_at is None
                assert sensor.boundary_payload_id is not None
                prior_id = latest.payload_id
            repeated = await save(factory, file, [observation()], 1)
            assert repeated["storage"]["observations_written"] == 0
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                assert latest.payload_id == prior_id and latest.received_at == START
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_latest_time_and_provider_revision_update_without_ranking_data_no(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        file = sensor_file()
        try:
            await boundary(factory)
            await save(factory, file, [observation(DATA_NO="9")])
            newer = observation(
                DATA_NO="1",
                SENSING_TIME="2026-11-04_09:10:00",
                DATE="2026-11-04 09:20:00",
                VISITOR_COUNT="8",
            )
            assert (await save(factory, file, [newer], 1))["storage"]["observations_written"] == 1
            corrected = {
                **newer,
                "DATE": "2026-11-04 09:21:00",
                "VISITOR_COUNT": "5",
                "DATA_NO": "0",
            }
            assert (await save(factory, file, [corrected], 2))["storage"][
                "observations_written"
            ] == 1
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                original_payload = latest.payload_id
                assert latest.count_10min == 5 and latest.data_no == "0"
            for step, row, reason in [
                (3, newer, "provider_time_guard"),
                (4, {**corrected, "DATA_NO": "10"}, None),
                (5, {**corrected, "VISITOR_COUNT": "99"}, "same_time_count_conflict"),
            ]:
                result = await save(factory, file, [row], step)
                assert result["storage"]["observations_written"] == 0
                async with factory() as s:
                    latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                    assert latest.payload_id == original_payload and latest.count_10min == 5
                    assert latest.received_at == START + timedelta(minutes=2)
                    assert latest.attempt_reason == reason
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case,status,reason",
    [
        ("unobserved", "unobserved", "unobserved"),
        ("invalid", "invalid", "invalid"),
        ("conflict", "conflicting", "conflicting"),
        ("old", "held", "observation_time_guard"),
        ("same_time", "conflicting", "same_time_count_conflict"),
        ("partial", "partial", "partial"),
        ("failed", "failed", "collection_incomplete"),
    ],
)
def test_bad_or_older_attempt_preserves_last_valid(normalized_database, case, status, reason):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        file = sensor_file()
        try:
            await boundary(factory)
            await save(factory, file, [observation()])
            async with factory() as s:
                prior = (await s.get(PedestrianSensorLatest, ("sdot_people", "1"))).payload_id
            rows = [
                observation(
                    SENSING_TIME="2026-11-04 09:10", DATE="2026-11-04 09:11", VISITOR_COUNT="8"
                )
            ]
            if case in {"unobserved", "failed"}:
                rows = []
            elif case == "invalid":
                rows[0]["VISITOR_COUNT"] = True
            elif case == "conflict":
                rows.append({**rows[0], "VISITOR_COUNT": "9"})
            elif case == "old":
                rows[0]["SENSING_TIME"] = "2026-11-04 08:00"
            elif case == "same_time":
                rows[0].update(SENSING_TIME="2026-11-04 09:00", DATE="2026-11-04 09:01")
            elif case == "partial":
                rows[0]["DATE"] = None
            data = collected(rows, (START + timedelta(minutes=1)).isoformat())
            if case == "failed":
                data["issues"] = ["provider_http_error"]
                data["pages"] = [{"error_code": "provider_http_error"}]
            result = await save(factory, file, rows, 1, collection=data)
            assert result["storage"]["observations_written"] == 0
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                assert latest.count_10min == 0 and latest.payload_id == prior
                assert latest.attempt_status == status and latest.attempt_reason == reason
                assert latest.attempt_at == START + timedelta(minutes=1)
            delayed = await save(factory, file, [observation(VISITOR_COUNT="99")], -1)
            assert delayed["error_code"] == "sensor_reference_time_guard"
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                assert latest.attempt_status == status and latest.payload_id == prior
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_only_complete_valid_reference_deactivates_and_keeps_old_observation_fk(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            first = sensor_file([location(), location("2")])
            await save(factory, first, [observation("2")])
            partial = sensor_file([location()], complete=False)
            result = await save(factory, partial, [], 1)
            assert result["storage"]["deactivated"] == 0
            async with factory() as s:
                assert (await s.get(PedestrianSensor, ("sdot_people", "2"))).is_active
            invalid = sensor_file([location(), location("1", E="37.6")])
            assert (await save(factory, invalid, [], 2))["storage"]["deactivated"] == 0
            assert (await save(factory, sensor_file([]), [], 3))["storage"]["deactivated"] == 0
            result = await save(factory, sensor_file([location()]), [], 4)
            assert result["storage"]["deactivated"] == 1
            async with factory() as s:
                assert not (await s.get(PedestrianSensor, ("sdot_people", "2"))).is_active
                assert (await s.get(PedestrianSensorLatest, ("sdot_people", "2"))).count_10min == 0
            older = sensor_file([location("3")], reference_date="2024-01-01")
            assert (await save(factory, older, [], 5))[
                "error_code"
            ] == "unverified_or_older_sensor_reference"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_storage_failure_rolls_back_writes_but_retains_file_and_pages(
    normalized_database, monkeypatch
):
    original = SensorWriter.store

    def fail(self, *args):
        original(self, *args)
        raise RuntimeError("test rollback")

    monkeypatch.setattr(SensorWriter, "store", fail)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            result = await save(factory, sensor_file(), [observation()])
            assert (
                result["status"] == "failed"
                and result["error_code"] == "sensor_normalization_failed"
            )
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(PedestrianSensor)) == 0
                assert await s.scalar(select(func.count()).select_from(PedestrianSensorLatest)) == 0
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .where(RawPayload.payload_kind == "source_file")
                    )
                    == 1
                )
                assert (
                    await s.scalar(
                        select(func.count())
                        .select_from(RawPayload)
                        .where(RawPayload.payload_kind == "provider_response")
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_new_observation_and_reference_change_preserve_provenance(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        first = sensor_file()
        try:
            await boundary(factory)
            await save(factory, first, [observation()])
            changed = sensor_file([location(C="replaced-device")], reference_date="2026-01-01")
            held = await save(factory, changed, [observation()], 1)
            assert held["storage"]["observations_written"] == 0
            assert not held["storage"]["sensor_results"][0]["matches_current_reference"]
            rows = [
                observation(
                    SENSING_TIME="2026-11-04 09:30", DATE="2026-11-04 09:31", VISITOR_COUNT="8"
                )
            ]
            updated = await save(factory, changed, rows, 2)
            assert updated["storage"]["observations_written"] == 1
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                sensor = await s.get(PedestrianSensor, ("sdot_people", "1"))
                assert latest.count_10min == 8 and latest.reference_version == sensor.source_version
                assert latest.reference_payload_id == sensor.reference_payload_id
                assert sensor.device_serial == "replaced-device"
            # A later cycle can carry an earlier response; it cannot replace the valid receipt.
            stale_receipt = collected(
                [observation(SENSING_TIME="2026-11-04 09:40", DATE="2026-11-04 09:41")],
                START.isoformat(),
            )
            result = await save(factory, changed, [], 3, collection=stale_receipt)
            assert (
                result["storage"]["sensor_results"][0]["reason"] == "observation_receipt_time_guard"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_cycles_cannot_restore_older_measurement(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        file = sensor_file()
        try:
            await boundary(factory)
            await asyncio.gather(
                save(factory, file, [observation(VISITOR_COUNT="1")], 0),
                save(
                    factory,
                    file,
                    [
                        observation(
                            VISITOR_COUNT="2",
                            SENSING_TIME="2026-11-04 09:30",
                            DATE="2026-11-04 09:31",
                        )
                    ],
                    1,
                ),
            )
            async with factory() as s:
                latest = await s.get(PedestrianSensorLatest, ("sdot_people", "1"))
                assert latest.count_10min == 2 and latest.attempt_at == START + timedelta(minutes=1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_wrong_page_evidence_cannot_create_observations(normalized_database):
    from copy import deepcopy

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await boundary(factory)
            data = collected([observation()])
            data["rows"] = deepcopy(data["rows"])
            data["rows"][0]["VISITOR_COUNT"] = "77"
            result = await save(factory, sensor_file(), [], collection=data)
            assert (
                result["status"] == "failed"
                and result["error_code"] == "sensor_page_evidence_mismatch"
            )
            async with factory() as s:
                assert await s.scalar(select(func.count()).select_from(PedestrianSensorLatest)) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
