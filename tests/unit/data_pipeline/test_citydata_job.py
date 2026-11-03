import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.modules.data_pipeline.collectors.citydata import read_citydata_areas
from app.modules.data_pipeline.pipelines import citydata
from jobs import collect_citydata, collect_citydata_all, project_citydata_parking


@pytest.mark.parametrize(
    ("area_statuses", "parking_status", "expected"),
    [
        (["success", "success"], "success", "success"),
        (["success", "success"], "partial", "partial"),
        (["success", "success"], "failed", "failed"),
        (["success", "success"], "skipped", "partial"),
        (["success", "partial"], "success", "partial"),
        (["success", "failed"], "success", "failed"),
        (["success", "skipped"], "success", "partial"),
        (["skipped", "skipped"], "skipped", "skipped"),
    ],
)
def test_batch_outcome_includes_parking_and_preserves_area_counts(
    monkeypatch, area_statuses, parking_status, expected
):
    targets = {"POI001": "first", "POI002": "second"}
    calls, reports = [], []

    async def fetch(name):
        calls.append(name)
        return {"AREA_NM": name}

    async def collect(engine, fetch_area, name):
        assert (await fetch_area())["AREA_NM"] == name
        return {"status": area_statuses[list(targets.values()).index(name)]}

    async def project(engine):
        calls.append("parking")
        return {"status": parking_status}

    monkeypatch.setattr(citydata, "run_citydata", collect)
    monkeypatch.setattr(citydata, "project_citydata_parking", project)
    monkeypatch.setattr(citydata.asyncio, "sleep", AsyncMock())
    result = asyncio.run(citydata.run_citydata_batch(None, fetch, targets, report=reports.append))
    assert calls == ["first", "second", "parking"]
    assert result["status"] == expected
    assert result["parking_projection"]["status"] == parking_status
    assert [r["status"] for r in reports] == area_statuses
    assert [r["area_code"] for r in reports] == list(targets)
    for status in ("success", "partial", "failed", "skipped"):
        assert result[status] == area_statuses.count(status)


def test_projection_exception_fails_cycle_without_exposing_details(monkeypatch):
    monkeypatch.setattr(citydata, "run_citydata", AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(
        citydata, "project_citydata_parking", AsyncMock(side_effect=RuntimeError("private details"))
    )
    monkeypatch.setattr(citydata.asyncio, "sleep", AsyncMock())
    result = asyncio.run(citydata.run_citydata_batch(None, AsyncMock(), {"POI001": "test"}))
    assert result["status"] == "failed"
    assert result["parking_projection"] == {
        "status": "failed",
        "error_code": "parking_projection_failed",
    }


@pytest.mark.parametrize("job", [collect_citydata, collect_citydata_all, project_citydata_parking])
@pytest.mark.parametrize(
    "status, expected", [("success", 0), ("failed", 1), ("partial", 2), ("skipped", 3)]
)
def test_jobs_propagate_pipeline_exit_status_and_close_engine(monkeypatch, job, status, expected):
    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    monkeypatch.setattr(job, "create_engine", lambda settings: engine)
    if job is collect_citydata:
        monkeypatch.setattr(job, "run_citydata", AsyncMock(return_value={"status": status}))
        code = asyncio.run(job.execute("test"))
    elif job is collect_citydata_all:
        monkeypatch.setattr(job, "read_citydata_areas", lambda path: {"POI001": "test"})
        monkeypatch.setattr(job, "run_citydata_batch", AsyncMock(return_value={"status": status}))
        code = asyncio.run(job.execute(0))
    else:
        monkeypatch.setattr(
            job, "project_citydata_parking", AsyncMock(return_value={"status": status})
        )
        code = asyncio.run(job.execute())
    assert code == expected
    assert engine.disposed


def test_official_citydata_area_file_still_reads_all_121_targets():
    path = Path(__file__).resolve().parents[3] / "data/reference/seoul_areas.xlsx"
    targets = read_citydata_areas(path)
    assert len(targets) == 121
    assert all(code.startswith("POI") and name.strip() for code, name in targets.items())
