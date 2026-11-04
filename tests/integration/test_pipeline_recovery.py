"""Failure recovery, current-use decisions and reference replacement in isolated PostGIS."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError, SeoulClient
from app.models import (
    AdministrativeArea,
    CollectionRun,
    MapRoadObservation,
    ParkingAvailabilityLatest,
    ParkingSourceRecord,
    RoadAreaMapping,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.collectors.citydata import collect_citydata
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
)
from app.modules.data_pipeline.pipelines import topis
from app.modules.data_pipeline.pipelines.parking import run_municipal_parking, run_parking
from app.repositories import road_reference
from app.repositories.parking import ParkingRepository
from jobs import scheduler
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration
LINK = "1000000000"


def registered(code="1", capacity="10"):
    return dict(PKLT_CD=code, PKLT_NM="parking" + code, LAT=37.56, LOT=126.97, TPKCT=capacity)


async def roads(factory, ids=(LINK,)):
    async with factory.begin() as s:
        result = await save_snapshot(
            s,
            "topis_geometry:recovery",
            {
                "features": [
                    {
                        "id": link,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [127.01, 37.5]],
                        },
                    }
                    for link in ids
                ]
            },
        )
        assert result.status == "success"


def traffic(link, speed):
    return {"rows": [{"link_id": link, "prcs_spd": str(speed), "prcs_trv_time": "30"}]}


@pytest.mark.parametrize("source", [CITYDATA_PARKING_SOURCE, MUNICIPAL_PARKING_SOURCE])
def test_parking_expiry_and_withdrawal_preserve_historical_facts(normalized_database, source):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        observed = datetime.now(UTC) - timedelta(minutes=30)
        row = (
            dict(
                PRK_CD="1",
                PRK_NM="parking",
                LAT=37.56,
                LNG=126.97,
                CPCTY="10",
                CUR_PRK_YN="Y",
                CUR_PRK_CNT="4",
                CUR_PRK_TIME=observed.isoformat(),
            )
            if source == CITYDATA_PARKING_SOURCE
            else {
                **registered(),
                "PRK_STTS_YN": "1",
                "PRK_STTS_NM": "현재주차대수 연계중",
                "NOW_PRK_VHCL_CNT": "6",
                "NOW_PRK_VHCL_UPDT_TM": observed.isoformat(),
            }
        )

        async def fetch():
            return [dict(row)]

        try:
            assert (await run_parking(engine, fetch, source))["accepted"] == 1
            async with factory() as s:
                record = await s.scalar(select(ParkingSourceRecord))
                identity = record.source_record_id
                repository = ParkingRepository(s)
                fresh = await repository.availability(identity, now=observed + timedelta(hours=1))
                assert fresh.usable and fresh.available_spaces == 4
                assert fresh.expires_at == observed + timedelta(hours=1)
                for now, reason in (
                    (observed + timedelta(hours=1, seconds=1), "STALE_DATA"),
                    (observed - timedelta(seconds=1), "FUTURE_TIMESTAMP"),
                ):
                    value = await repository.availability(identity, now=now)
                    assert not value.usable and value.available_spaces is None
                    assert value.reason_code == reason
            if source == CITYDATA_PARKING_SOURCE:
                row["CUR_PRK_YN"] = "N"
            else:
                row.update(PRK_STTS_YN="0", PRK_STTS_NM="미연계중")
            await run_parking(engine, fetch, source)
            async with factory() as s:
                state = await s.get(ParkingAvailabilityLatest, identity)
                value = await ParkingRepository(s).availability(identity)
                assert state.usable and state.observed_at == observed
                assert state.attempt_status == "not_provided"
                assert not value.usable and value.available_spaces is None
                assert value.reason_code == "NOT_PROVIDED"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("municipal", [False, True])
def test_parking_partial_pages_update_successes_without_retiring_missing_rows(
    normalized_database, municipal
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        baseline_rows = [registered(str(n)) for n in range(1, 4)]

        async def baseline():
            return baseline_rows

        visited = []

        async def page(start, end):
            visited.append(start)
            if start == 2:
                raise CollectionError("provider_network_error")
            row = registered(str(start), "20")
            if municipal:
                row.pop("LAT")
                row.pop("LOT")
            return 3, [row]

        try:
            await run_parking(engine, baseline, SOURCE)
            if municipal:
                await run_parking(engine, baseline, MUNICIPAL_PARKING_SOURCE)

            async def fetch():
                return await collect_parking(SimpleNamespace(fetch_page=page), page_size=1)

            source = MUNICIPAL_PARKING_SOURCE if municipal else SOURCE
            result = (
                await run_municipal_parking(engine, fetch)
                if municipal
                else await run_parking(engine, fetch, SOURCE)
            )
            assert visited == [1, 2, 3]
            assert result["status"] == "partial" and result["accepted"] == 2
            assert result["error_code"] == "provider_network_error"
            async with factory() as s:
                records = {
                    r.provider_record_id: r
                    for r in await s.scalars(
                        select(ParkingSourceRecord).where(ParkingSourceRecord.source == source)
                    )
                }
                assert {k: r.registered_capacity for k, r in records.items()} == {
                    "1": 20,
                    "2": 10,
                    "3": 20,
                }
                assert all(r.is_active for r in records.values())
                run = await s.get(CollectionRun, UUID(result["run_id"]))
                assert run.completeness == "partial" and run.error_code == "provider_network_error"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "exit", "cancel"])
def test_scheduler_publishes_committed_success_after_collector_interruption(
    normalized_database, monkeypatch, failure
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "CONCURRENCY", 1)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        second_started = asyncio.Event()
        try:
            await roads(factory, (LINK, "1000000001"))
            async with factory.begin() as s:
                await save_snapshot(s, "topis:" + LINK, traffic(LINK, 10))

            async def fetch(link):
                if link == LINK:
                    return traffic(link, 25)
                second_started.set()
                await asyncio.Event().wait()

            async def command(task):
                collector = asyncio.create_task(
                    topis.run_topis(engine, fetch, [LINK, "1000000001"])
                )
                try:
                    await asyncio.wait_for(second_started.wait(), 5)
                finally:
                    collector.cancel()
                    await asyncio.gather(collector, return_exceptions=True)
                if failure == "timeout":
                    raise TimeoutError
                if failure == "cancel":
                    raise asyncio.CancelledError
                return -9

            monkeypatch.setattr(scheduler, "run_command", command)
            if failure in {"timeout", "cancel"}:
                error = TimeoutError if failure == "timeout" else asyncio.CancelledError
                with pytest.raises(error):
                    await scheduler.cycle(engine, "topis", scheduler.TASKS["topis"])
            else:
                assert (
                    await scheduler.cycle(engine, "topis", scheduler.TASKS["topis"])
                ).status == "failed"
            async with factory() as s:
                canonical = await s.get(RoadTrafficLatest, ("topis_traffic", LINK))
                public = await s.get(MapRoadObservation, LINK)
                assert canonical.speed_kmh == public.speed_kmh == 25
                assert canonical.payload_id == public.payload_id
                assert public.observed_at is None

            # A restart repairs a previous process death before issuing any new API request.
            async with factory.begin() as s:
                await save_snapshot(s, "topis:" + LINK, traffic(LINK, 30))

            async def restarted(task):
                async with factory() as s:
                    assert (await s.get(MapRoadObservation, LINK)).speed_kmh == 30
                return 0

            monkeypatch.setattr(scheduler, "run_command", restarted)
            assert (
                await scheduler.cycle(engine, "topis", scheduler.TASKS["topis"])
            ).status == "success"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("error_code", ["ERROR-500", "ERROR-600"])
def test_documented_provider_body_server_errors_have_bounded_retries(
    normalized_database, monkeypatch, error_code
):
    monkeypatch.setattr(topis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(topis, "RETRY_DELAYS", (0, 0))

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        calls = {"topis": 0, "parking": 0, "citydata": 0}

        async def respond(request):
            if "/TrafficInfo/" in request.url.path:
                calls["topis"] += 1
                return httpx.Response(200, text=f"<RESULT><CODE>{error_code}</CODE></RESULT>")
            source = "citydata" if "/citydata/" in request.url.path else "parking"
            calls[source] += 1
            return httpx.Response(200, json={"RESULT": {"CODE": error_code}})

        try:
            await roads(factory)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                result = await topis.run_topis(
                    engine, lambda link: collect_topis(http, "test", link), [LINK]
                )
                assert result["retry_attempts"] == 2
                assert result["counts"] == {"provider_server_error": 1}
                with pytest.raises(CollectionError, match="provider_server_error"):
                    await SeoulClient(http, "test").fetch_page(1, 1000)
                with pytest.raises(CollectionError, match="provider_server_error"):
                    await collect_citydata(http, "test", "test", expected_code="POI001")
            assert calls == {"topis": 3, "parking": 3, "citydata": 3}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sgis_replaces_complete_references_and_relations_but_preserves_partial_input(
    normalized_database,
    monkeypatch,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)

        async def area(code, x, *, complete=False, expected="success"):
            async with factory.begin() as s:
                result = await save_snapshot(
                    s,
                    "sgis_areas",
                    {
                        "complete": complete,
                        "features": [
                            {
                                "id": code,
                                "properties": {"name": code, "sigungu_code": "district"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [
                                            [x, 37.4],
                                            [x + 0.1, 37.4],
                                            [x + 0.1, 37.6],
                                            [x, 37.6],
                                            [x, 37.4],
                                        ]
                                    ],
                                },
                            }
                        ],
                    },
                )
                assert result.status == expected

        try:
            await area("a", 126.99, complete=True)
            await roads(factory)
            async with factory() as s:
                assert await s.get(RoadAreaMapping, (LINK, "a")) is not None
            await area("a", 128, complete=True)
            async with factory() as s:
                assert await s.get(RoadAreaMapping, (LINK, "a")) is None
            await area("b", 126.99)  # Not a proven complete reference.
            async with factory() as s:
                assert (await s.get(AdministrativeArea, "a")).is_active
                b = await s.get(AdministrativeArea, "b")
                relation = await s.get(RoadAreaMapping, (LINK, "b"))
                assert relation.boundary_version == b.boundary_version
            await area("b", 126.99, complete=True)
            async with factory() as s:
                assert not (await s.get(AdministrativeArea, "a")).is_active
                relation = await s.get(RoadAreaMapping, (LINK, "b"))
                mapped_at = relation.mapped_at
            await area("b", 126.99, complete=True)
            async with factory() as s:
                assert (await s.get(RoadAreaMapping, (LINK, "b"))).mapped_at == mapped_at
            # Older processors could leave a missing relation even when boundaries are unchanged.
            async with factory.begin() as s:
                await s.execute(delete(RoadAreaMapping))
                (
                    await s.get(AdministrativeArea, "b")
                ).processor_version = "normalized-2026-11-04.15"
            await area("b", 126.99, complete=True)
            async with factory() as s:
                relation = await s.get(RoadAreaMapping, (LINK, "b"))
                assert relation is not None
                mapped_at = relation.mapped_at
                previous = await s.get(AdministrativeArea, "b")
                original_geometry, original_payload = (
                    str(previous.geometry),
                    previous.last_payload_id,
                )

            def broken_mapping(*args, **kwargs):
                raise RuntimeError("relation replacement failed")

            with monkeypatch.context() as patch:
                patch.setattr(road_reference, "rebuild_road_areas", broken_mapping)
                await area("b", 128, complete=True, expected="failed")
            async with factory() as s:
                current = await s.get(AdministrativeArea, "b")
                assert (str(current.geometry), current.last_payload_id) == (
                    original_geometry,
                    original_payload,
                )
                assert (await s.get(RoadAreaMapping, (LINK, "b"))).mapped_at == mapped_at
        finally:
            await engine.dispose()

    asyncio.run(run())
