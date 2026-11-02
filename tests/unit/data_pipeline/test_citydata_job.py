import asyncio

import pytest

from app.core.config import Settings
from jobs import collect_citydata_all as job


@pytest.mark.parametrize("status, expected", [("success", True), ("failed", False)])
def test_single_cycle_reports_collection_failure(monkeypatch, status, expected):
    class Engine:
        async def dispose(self):
            pass

    async def collect(*args):
        return {"status": status}

    async def project(*args):
        return {}

    async def sleep(*args):
        pass

    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    monkeypatch.setattr(job, "areas", lambda: {"POI001": "test"})
    monkeypatch.setattr(job, "create_engine", lambda settings: Engine())
    monkeypatch.setattr(job, "run_citydata", collect)
    monkeypatch.setattr(job, "project", project)
    monkeypatch.setattr(job.asyncio, "sleep", sleep)
    assert asyncio.run(job.main_async(0)) is expected
