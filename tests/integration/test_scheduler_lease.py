import asyncio
import os
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.session import create_engine
from jobs import scheduler


@pytest.mark.integration
@pytest.mark.parametrize("exit_code", [0, 2])
def test_db_lease_prevents_concurrent_workers_and_releases(monkeypatch, exit_code):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")

    async def run():
        engine = create_engine(Settings(_env_file=None, database_url=url))
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def command(task):
            calls.append(task)
            entered.set()
            await release.wait()
            return exit_code

        monkeypatch.setattr(scheduler, "run_command", command)
        name = "test_" + uuid4().hex
        task = scheduler.TASKS["citydata"]
        first = asyncio.create_task(scheduler.cycle(engine, name, task))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await scheduler.cycle(engine, name, task)
            assert len(calls) == 1
            release.set()
            assert await first
            assert await scheduler.cycle(engine, name, task)
            assert len(calls) == 2
        finally:
            release.set()
            await first
            await engine.dispose()

    asyncio.run(run())
