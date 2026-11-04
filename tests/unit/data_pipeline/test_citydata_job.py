import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.modules.data_pipeline.collectors.official_files import read_citydata_areas
from jobs import collect_citydata, collect_citydata_all


@pytest.mark.parametrize("job", [collect_citydata, collect_citydata_all])
@pytest.mark.parametrize(
    "status, expected",
    [("success", 0), ("failed", 1), ("partial", 2), ("skipped", 3), ("paused_provider_limit", 4)],
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
        mock_db_targets(monkeypatch, job, {"POI001": "test"})
        monkeypatch.setattr(job, "run_citydata", AsyncMock(return_value={"status": status}))
        code = asyncio.run(job.execute("test"))
    elif job is collect_citydata_all:
        mock_db_targets(monkeypatch, job, {"POI001": "test"})
        monkeypatch.setattr(job, "run_citydata_batch", AsyncMock(return_value={"status": status}))
        code = asyncio.run(job.execute(0))
    assert code == expected
    assert engine.disposed


def test_official_citydata_area_file_still_reads_all_121_targets():
    path = Path(__file__).resolve().parents[3] / "data/seoul_areas.xlsx"
    targets = read_citydata_areas(path)
    assert len(targets) == 121
    assert all(code.startswith("POI") and name.strip() for code, name in targets.items())


@pytest.mark.parametrize("status,delay", [("success", 300), ("paused_provider_limit", 3600)])
def test_explicit_citydata_repeat_respects_provider_pause(monkeypatch, status, delay):
    job = collect_citydata_all
    engine = AsyncMock()
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    monkeypatch.setattr(job, "create_engine", lambda settings: engine)
    mock_db_targets(monkeypatch, job, {"POI001": "test"})
    monkeypatch.setattr(job, "run_citydata_batch", AsyncMock(return_value={"status": status}))
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(job.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(job.execute(300))
    sleep.assert_awaited_once_with(delay)
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("requested", ["POI009", "광화문·덕수궁"])
def test_single_job_passes_official_code_and_name_to_collector_and_pipeline(monkeypatch, requested):
    job = collect_citydata
    engine = AsyncMock()
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    monkeypatch.setattr(job, "create_engine", lambda settings: engine)
    mock_db_targets(monkeypatch, job, {"POI009": "광화문·덕수궁"})
    collector = AsyncMock(return_value={"AREA_CD": "POI009"})
    monkeypatch.setattr(job, "collect_citydata", collector)

    async def pipeline(db, fetch, name, *, expected_code):
        assert db is engine and name == "광화문·덕수궁" and expected_code == "POI009"
        assert (await fetch())["AREA_CD"] == expected_code
        return {"status": "success"}

    monkeypatch.setattr(job, "run_citydata", pipeline)
    assert asyncio.run(job.execute(requested)) == 0
    assert collector.await_args.args[2] == "광화문·덕수궁"
    assert collector.await_args.kwargs == {"expected_code": "POI009"}
    engine.dispose.assert_awaited_once()


def mock_db_targets(monkeypatch, job, targets):
    monkeypatch.setattr(job, "async_sessionmaker", lambda engine: lambda: AsyncMock())
    monkeypatch.setattr(job.ReferenceRepository, "citydata_areas", AsyncMock(return_value=targets))


@pytest.mark.parametrize("job", [collect_citydata, collect_citydata_all])
def test_missing_imported_reference_does_not_read_files_or_call_provider(monkeypatch, job):
    engine = AsyncMock()
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    monkeypatch.setattr(job, "create_engine", lambda _: engine)
    mock_db_targets(monkeypatch, job, {})
    name = "run_citydata" if job is collect_citydata else "run_citydata_batch"
    pipeline = AsyncMock()
    monkeypatch.setattr(job, name, pipeline)
    assert asyncio.run(job.execute("POI001") if job is collect_citydata else job.execute()) == 1
    pipeline.assert_not_awaited()
    engine.dispose.assert_awaited_once()
