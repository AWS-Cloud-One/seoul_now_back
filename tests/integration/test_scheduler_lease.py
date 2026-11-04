import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from jobs import scheduler


@pytest.mark.integration
@pytest.mark.parametrize("exit_code", [0, 2, 4])
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
        task = replace(scheduler.TASKS["citydata"], prerequisites=())
        first = asyncio.create_task(scheduler.cycle(engine, name, task))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await scheduler.cycle(engine, name, task) == scheduler.TaskResult(
                "skipped", 3, "already_running"
            )
            assert len(calls) == 1
            release.set()
            status = {0: "success", 2: "partial", 4: "paused_provider_limit"}[exit_code]
            assert await first == scheduler.TaskResult(status, exit_code)
            assert await scheduler.cycle(engine, name, task) == scheduler.TaskResult(
                status, exit_code
            )
            assert len(calls) == 2
        finally:
            release.set()
            await first
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
@pytest.mark.parametrize(
    "area_exit,existing_boundary,expected_exit,consumers_run",
    [(0, False, 0, True), (2, False, 2, True), (1, True, 1, True), (1, False, 1, False)],
)
def test_once_uses_boundary_after_selected_reference_attempt(
    normalized_database, monkeypatch, area_exit, existing_boundary, expected_exit, consumers_run
):
    settings = Settings(
        _env_file=None,
        database_url=normalized_database,
        seoul_sdot_api_key="test",
        sgis_consumer_key="test",
        sgis_consumer_secret="test",
    )
    monkeypatch.setattr(scheduler, "Settings", lambda: settings)

    async def run():
        engine = create_engine(settings)
        factory = async_sessionmaker(engine)
        executed = []

        async def store_boundary():
            async with factory.begin() as session:
                await session.execute(
                    text("""
                    INSERT INTO data_sources(source_code,display_name,provider_name)
                    VALUES ('sgis_seoul_boundary','test','test'),('sdot_people','test','test')
                    ON CONFLICT DO NOTHING
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO region_boundaries(code_system,region_code,name,source,
                        source_version,boundary_version,geometry,processed_at,processor_version)
                    VALUES ('sgis','11','Seoul','sgis_seoul_boundary','2025','v1',
                        ST_GeomFromText('MULTIPOLYGON(((127 37.5,127.1 37.5,127.1 37.6,127 37.5)))',
                            4326),now(),'test')
                    ON CONFLICT DO NOTHING
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO pedestrian_sensors(source,sensor_code,provider_sensor_id,
                        device_serial,address,geometry,source_version,boundary_version,
                        reference_checked_at,processed_at,processor_version)
                    VALUES ('sdot_people','1','test','test','test',
                            ST_SetSRID(ST_Point(127.01,37.51),4326),
                        'v1','v1',now(),now(),'test') ON CONFLICT DO NOTHING
                """)
                )

        async def command(task):
            target = task.args[0]
            executed.append(target)
            if target == "areas":
                await asyncio.sleep(0)
                if area_exit in {0, 2}:
                    await store_boundary()
                return area_exit
            assert "areas" in executed
            return 0

        monkeypatch.setattr(scheduler, "run_command", command)
        try:
            if existing_boundary:
                await store_boundary()
            assert await scheduler.execute(["sdot", "areas"], once=True) == expected_exit
            assert executed[0] == "areas"
            assert set(executed) == ({"areas", "sdot"} if consumers_run else {"areas"})
        finally:
            await engine.dispose()

    asyncio.run(run())
