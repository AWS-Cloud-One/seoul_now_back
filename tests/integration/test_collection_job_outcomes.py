"""Jobs must report persisted outcomes without losing failed input or duplicating attempts."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models import CollectionRun, RawPayload
from app.modules.data_pipeline.collectors import official_files as official_collectors
from app.modules.data_pipeline.collectors import supplemental as supplemental_collectors
from app.modules.data_pipeline.pipelines import national_parking, snapshots, topis_storage
from app.services.map_publication import MapPublicationService
from app.storage.payloads import configured_store
from jobs import collect_supplemental, collect_topis, import_place_boundaries
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture
def job_database(normalized_database, monkeypatch):
    settings = Settings(_env_file=None, database_url=normalized_database)
    for module in (collect_supplemental, collect_topis, import_place_boundaries):
        monkeypatch.setattr(module, "Settings", lambda: settings)
    return normalized_database


async def assert_saved(factory, source, status, error=None, *, target="all", payloads=1):
    async with factory() as session:
        runs = list(
            await session.scalars(
                select(CollectionRun).where(
                    CollectionRun.source == source, CollectionRun.target_key == target
                )
            )
        )
        assert len(runs) == 1  # No extra success/failure attempt hiding the normalized run.
        assert (runs[0].status, runs[0].error_code) == (status, error)
        assert (
            await session.scalar(
                select(func.count()).select_from(RawPayload).where(RawPayload.run_id == runs[0].id)
            )
            == payloads
        )


def fail_normalization_for(monkeypatch, source):
    original = snapshots.normalize_snapshot

    def normalize(session, key, *args, **kwargs):
        if key == source:
            raise ValueError("private provider details must not be printed")
        return original(session, key, *args, **kwargs)

    monkeypatch.setattr(snapshots, "normalize_snapshot", normalize)


@pytest.mark.parametrize(
    ("status", "error", "exit_code"),
    [
        ("success", None, 0),
        ("partial", "missing_road_geometry", 2),
        ("failed", "normalization_failed", 1),
    ],
)
def test_topis_exit_and_log_match_saved_outcome(
    job_database, monkeypatch, capsys, status, error, exit_code
):
    async def fetch(*args):
        return {"rows": [{"link_id": "r1", "prcs_spd": "0", "prcs_trv_time": "0"}]}

    monkeypatch.setattr(collect_topis, "collect_topis", fetch)

    async def run():
        engine = create_async_engine(job_database)
        factory = async_sessionmaker(engine)
        try:
            if status != "partial":
                async with factory.begin() as session:
                    saved = await save_snapshot(
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
                    assert saved.status == "success"
                    await MapPublicationService(session).publish("roads")
            if status == "failed":

                def fail_traffic(*args, **kwargs):
                    raise ValueError("private provider details must not be printed")

                monkeypatch.setattr(topis_storage, "normalize_traffic", fail_traffic)
            assert await collect_topis.execute(["r1"]) == exit_code
            await assert_saved(factory, "topis_traffic", status, target="network", payloads=1)
            async with factory() as session:
                payload = await session.scalar(
                    select(RawPayload).where(RawPayload.source == "topis_traffic")
                )
                item = configured_store().load_json(payload)["entries"][0]
                assert (item["status"], item["error_code"]) == (status, error)
        finally:
            await engine.dispose()

    asyncio.run(run())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1 and lines[0]["status"] == status
    assert lines[0]["counts"] == {error or status: 1}


@pytest.mark.parametrize("status", ["success", "partial", "failed"])
def test_supplemental_reports_normalized_and_incomplete_outcomes(
    job_database, monkeypatch, capsys, status
):
    async def fetch(*args, progress):
        assert progress == {}
        facility = {
            "prk_center_id": "p1",
            "prk_plce_nm": "parking",
            "prk_plce_adres_sido": "서울특별시",
            "prk_plce_entrc_la": "37.56",
            "prk_plce_entrc_lo": "126.97",
        }
        return {
            "datasets": {
                method: {
                    "rows": rows,
                    "status": "partial" if status == "partial" else "success",
                    "complete": status != "partial",
                    "provider_total": len(rows),
                    "error_code": "parking_page_limit" if status == "partial" else None,
                    "pages": [
                        {
                            "page": 1,
                            "received_at": "2020-01-01T00:00:00Z",
                            "usable": True,
                            "body": {method: rows},
                        }
                    ],
                }
                for method, rows in (
                    ("PrkSttusInfo", [facility]),
                    ("PrkOprInfo", []),
                    ("PrkRealtimeInfo", []),
                )
            }
        }

    monkeypatch.setattr(supplemental_collectors, "collect_national_parking", fetch)
    error = "normalization_failed" if status == "failed" else None
    if status == "failed":

        def fail(*args):
            raise RuntimeError("private provider details")

        monkeypatch.setattr(national_parking, "normalize_datasets", fail)

    async def run():
        engine = create_async_engine(job_database)
        try:
            code = await collect_supplemental.execute("parking")
            result = json.loads(capsys.readouterr().out)
            assert code == {"success": 0, "partial": 2, "failed": 1}[status]
            assert (result["status"], result["error_code"]) == (status, error)
            await assert_saved(
                async_sessionmaker(engine), "national_parking", status, error, payloads=4
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sgis_boundary_failure_is_not_hidden_by_successful_areas(job_database, monkeypatch, capsys):
    def response(request):
        path = request.url.path
        if path.endswith("authentication.json"):
            data = {"result": {"accessToken": "test-token"}}
        elif path.endswith("stage.json"):
            rows = (
                [{"cd": "11010", "addr_name": "test"}]
                if request.url.params["cd"] == "11"
                else [
                    {
                        "cd": "11010510",
                        "addr_name": "test",
                        "full_addr": "서울 test",
                        "pg": (
                            "POLYGON((953000 1950000,954000 1950000,954000 1951000,"
                            "953000 1951000,953000 1950000))"
                        ),
                    }
                ]
            )
            data = {"errCd": 0, "result": rows}
        else:
            assert path.endswith("hadmarea.geojson")
            # A point is rejected before storage; the valid administrative area still commits.
            data = {
                "errCd": 0,
                "features": [{"geometry": {"type": "Point", "coordinates": [953901, 1952032]}}],
            }
        return httpx.Response(200, json=data)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(response), **kw)
    )

    async def run():
        engine = create_async_engine(job_database)
        factory = async_sessionmaker(engine)
        try:
            assert await collect_supplemental.execute("areas") == 1
            result = json.loads(capsys.readouterr().out)
            assert result["status"] == "failed" and result["error_code"] == "no_usable_boundary"
            await assert_saved(
                factory, "sgis_seoul_boundary", "failed", "no_usable_boundary", payloads=2
            )
            await assert_saved(factory, "sgis_areas", "success", payloads=2)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boundary_import_failure_commits_evidence_and_exits_nonzero(
    job_database, monkeypatch, capsys
):
    monkeypatch.setattr(
        official_collectors,
        "read_shapes",
        lambda _: [
            (
                {"AREA_CD": "POI001", "AREA_NM": "테스트", "CATEGORY": "공원"},
                [[(127, 37.5), (127.1, 37.5), (127.1, 37.6), (127, 37.5)]],
            )
        ],
    )
    fail_normalization_for(monkeypatch, "citydata_place_boundaries")

    async def run():
        engine = create_async_engine(job_database)
        try:
            assert await import_place_boundaries.execute() == 1
            await assert_saved(
                async_sessionmaker(engine),
                "citydata_place_boundaries",
                "failed",
                "normalization_failed",
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed" and output["error_code"] == "normalization_failed"
