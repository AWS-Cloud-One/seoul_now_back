"""Supplemental requests run outside DB transactions and preserve stored data contracts."""

import asyncio

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import CollectionRun
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.modules.data_pipeline.pipelines.supplemental import SOURCES, run_supplemental
from app.repositories.collection import CollectionRepository
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration

BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[126.9, 37.4], [127.1, 37.4], [127.1, 37.7], [126.9, 37.7], [126.9, 37.4]]],
}
CCTV_ROWS = [
    {
        "roadsectionid": "inside",
        "coordx": "127",
        "coordy": "37.5",
        "cctvurl": "https://example.com/a",
    },
    {"roadsectionid": "outside", "coordx": "129", "coordy": "37.5"},
    {"roadsectionid": "invalid", "coordx": "NaN", "coordy": "37.5"},
    {"roadsectionid": "edge", "coordx": "126.9", "coordy": "37.4"},
]


def watch_transactions(engine):
    active = set()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        active.add(id(connection))

    @event.listens_for(engine.sync_engine, "commit")
    @event.listens_for(engine.sync_engine, "rollback")
    def end(connection):
        active.discard(id(connection))

    return active


async def seed_boundary(factory, year=2025):
    async with factory.begin() as session:
        result = await save_snapshot(
            session,
            "sgis_seoul_boundary",
            {
                "features": [{"geometry": BOUNDARY}],
                "boundary_year": year,
            },
        )
        assert result.status == "success"


@pytest.mark.parametrize("target", ["areas", "sdot", "cctv", "parking"])
def test_http_runs_without_open_transactions_and_saved_shapes_are_preserved(
    normalized_database, monkeypatch, target, stored_payload
):
    sensor_rows = [
        {"B": "1", "D": "서울특별시", "E": "37.5", "F": "127"},
        {"B": "2", "D": "서울특별시", "E": "37.5", "F": "129"},
        {"B": "3", "D": "서울특별시", "E": "37.4", "F": "126.9"},
        {"B": "4", "D": "서울특별시", "E": "37.5", "F": "127"},
    ]
    monkeypatch.setattr(collectors, "read_sensor_rows", lambda path: sensor_rows)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        active = watch_transactions(engine)
        calls = []
        settings = Settings(_env_file=None, seoul_sdot_api_key="test")

        def response(request):
            assert not active, (
                "HTTP must not wait inside a DB transaction, including the lock connection"
            )
            calls.append(request.url.path)
            if target == "sdot":
                rows = [
                    {"SERIAL": code, "SENSING_TIME": "2026-10-25_18:00:00", "VISITOR_COUNT": count}
                    for code, count in (("1", "0"), ("2", "9"), ("3", "4"), ("3", "5"))
                ]
                data = {
                    "sDoTPeople": {
                        "RESULT": {"CODE": "INFO-000"},
                        "list_total_count": 4,
                        "row": rows,
                    }
                }
            elif target == "cctv":
                data = {"response": {"data": CCTV_ROWS}}
            elif target == "parking":
                method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
                data = {"resultCode": "00", "totalCount": 1, method: [{"prk_center_id": "p1"}]}
            elif request.url.path.endswith("authentication.json"):
                data = {"result": {"accessToken": "test-token"}}
            elif request.url.path.endswith("stage.json"):
                rows = (
                    [{"cd": "1101", "addr_name": "테스트구"}]
                    if request.url.params["cd"] == "11"
                    else [
                        {
                            "cd": "11010",
                            "addr_name": "테스트동",
                            "full_addr": "서울 테스트구 테스트동",
                            "pg": (
                                "POLYGON((953000 1950000,954000 1950000,"
                                "954000 1951000,953000 1950000))"
                            ),
                        }
                    ]
                )
                data = {"errCd": 0, "result": rows}
            else:
                assert request.url.path.endswith("hadmarea.geojson")
                data = {
                    "errCd": 0,
                    "features": [
                        {
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [953000, 1950000],
                                        [954000, 1950000],
                                        [954000, 1951000],
                                        [953000, 1950000],
                                    ]
                                ],
                            }
                        }
                    ],
                }
            return httpx.Response(200, json=data)

        try:
            if target in {"sdot", "cctv"}:
                await seed_boundary(factory, year=2026)

            def read(*args):
                pytest.fail("supplemental reference lookup must not read payload files")

            with monkeypatch.context() as patch:
                patch.setattr(type(configured_store()), "load_json", read)
                async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                    result = await run_supplemental(
                        engine, http, settings, target, sensor_path="fixture.xlsx"
                    )
            assert result["status"] == "success"
            assert result["records"] == {"areas": 1, "sdot": 3, "cctv": 2, "parking": 3}[target]
            assert len(calls) == {"areas": 4, "sdot": 1, "cctv": 1, "parking": 3}[target]
            async with factory() as session:
                _, data = await stored_payload(session, SOURCES[target], "all")
                if target == "areas":
                    feature = data["features"][0]
                    assert feature["properties"]["sigungu_code"] == "1101"
                    assert feature["properties"]["district"] == "테스트구"
                    assert data["boundary_year"] is None
                    x, y = feature["geometry"]["coordinates"][0][0]
                    assert 126 < x < 128 and 37 < y < 38
                elif target == "sdot":
                    assert data["seoul_boundary_year"] == 2026
                    items = {item["id"]: item for item in data["items"]}
                    assert items["1"]["count_10min"] == 0
                    assert (
                        items["3"]["count_10min"] is None
                        and items["3"]["observation_status"] == "conflicting"
                    )
                    assert items["4"]["observed_at"] is None
                    assert data["excluded_outside_seoul"] == 1 and data["received"] == 4
                    assert data["scope"] == "recent_10000_rows" and data["complete_history"] is True
                elif target == "cctv":
                    assert [item["id"] for item in data["items"]] == ["inside:0", "edge:1"]
                    assert data["received"] == 4 and data["issued_at"].endswith("+00:00")
                    assert all(item["playback_status"] == "unverified" for item in data["items"])
                else:
                    assert all(dataset["complete"] for dataset in data["datasets"].values())
                    assert set(data["datasets"]) == {
                        "PrkSttusInfo",
                        "PrkOprInfo",
                        "PrkRealtimeInfo",
                    }
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("target", ["sdot", "cctv"])
def test_missing_boundary_fails_before_spending_api_calls(normalized_database, target):
    def response(request):
        pytest.fail("missing prerequisite must prevent HTTP collection")

    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                result = await run_supplemental(engine, http, Settings(_env_file=None), target)
            assert (
                result["status"] == "failed"
                and result["error_code"] == "collect_sgis_boundary_first"
            )
            async with async_sessionmaker(engine)() as session:
                record = await session.scalar(select(CollectionRun))
                assert record.source == SOURCES[target] and record.status == "failed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_held_source_lock_skips_without_http(normalized_database):
    def response(request):
        pytest.fail("a locked source must not be collected twice")

    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with engine.connect() as lock:
                await lock.execute(text("SELECT pg_advisory_lock(hashtext('national_parking'))"))
                await lock.commit()
                try:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                        result = await run_supplemental(
                            engine, http, Settings(_env_file=None), "parking"
                        )
                    assert result["status"] == "skipped"
                finally:
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(hashtext('national_parking'))")
                    )
                    await lock.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cctv_http_failure_preserves_previous_data_and_releases_lock(
    normalized_database, stored_payload
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        calls = 0

        def response(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                return httpx.Response(429, text="private provider details")
            return httpx.Response(200, json={"response": {"data": CCTV_ROWS}})

        try:
            await seed_boundary(factory)
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                settings = Settings(_env_file=None)
                assert (await run_supplemental(engine, http, settings, "cctv"))[
                    "status"
                ] == "success"
                async with factory() as session:
                    prior, _ = await stored_payload(session, "its_cctv", "all")
                failed = await run_supplemental(engine, http, settings, "cctv")
                assert (
                    failed["status"] == "failed"
                    and failed["error_code"] == "provider_or_database_error"
                )
                assert calls == 2  # A quota response must not trigger retries.
                async with factory() as session:
                    current, _ = await stored_payload(session, "its_cctv", "all")
                    assert current.payload_id == prior.payload_id
                    attempt = await CollectionRepository(session).latest_run("its_cctv", "all")
                    assert attempt.status == "failed"
                assert (await run_supplemental(engine, http, settings, "cctv"))[
                    "status"
                ] == "success"
                assert calls == 3
        finally:
            await engine.dispose()

    asyncio.run(run())
