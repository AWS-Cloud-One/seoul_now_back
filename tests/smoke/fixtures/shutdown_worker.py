"""Synthetic worker used only by the explicit container shutdown check."""

import asyncio
import os
import signal
import sys
import time
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.collection import CollectionRepository
from jobs import scheduler
from jobs.runtime import report, run_cli


def record_id(name):
    value = UUID(os.environ["TEST_RUN_ID"])
    return value if name == "parking" else uuid5(value, name)


async def child(scenario, name):
    if scenario == "quick":
        return 0
    if scenario == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    engine = create_engine(Settings())
    run_id = record_id(name)
    try:
        factory = async_sessionmaker(engine)
        async with factory.begin() as session:
            run = await CollectionRepository(session).start(
                "seoul_getparkinfo", str(run_id), run_id=run_id
            )
            run.accepted, run.received = 1, 1
            await session.execute(
                text("""
                INSERT INTO parking_facilities (parking_id, name, location)
                VALUES (:id, 'synthetic committed', ST_GeogFromText('POINT(126.978 37.5665)'))
            """),
                {"id": run_id},
            )
        async with factory.begin() as session:
            await session.execute(
                text("""
                INSERT INTO parking_facilities (parking_id, name, location)
                VALUES (:id, 'synthetic uncommitted', ST_GeogFromText('POINT(126.978 37.5665)'))
            """),
                {"id": uuid5(run_id, "uncommitted")},
            )
            try:
                report({"status": "child_ready", "pid": os.getpid(), "name": name})
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.3)
                report({"status": "child_cleanup_done", "pid": os.getpid()})
    finally:
        await engine.dispose()


def main():
    if sys.argv[1] == "child":
        run_cli(child, sys.argv[2], sys.argv[3])
    scenario = sys.argv[1]
    if scenario == "deadline":

        def blocking():
            report({"status": "executor_ready"})
            time.sleep(90)

        async def blocked():
            await asyncio.to_thread(blocking)

        run_cli(blocked)
    child_scenario = "quick" if scenario in {"idle", "restart"} else scenario
    names = ["parking"]
    if scenario == "dependency":
        names = ["areas", "sdot"]
    elif scenario == "parallel":
        names = ["parallel-a", "parallel-b", "parallel-c"]
    for name in names:
        scheduler.TASKS[name] = scheduler.Task(
            "shutdown_worker",
            600,
            ("seoul_api_key",),
            args=("child", child_scenario, name),
            after=("areas",) if name == "sdot" else (),
        )
    if scenario == "recovery":

        async def blocked_publication(*args, **kwargs):
            report({"status": "recovery_ready"})
            await asyncio.Event().wait()

        scheduler.publish_result = blocked_publication
    once = scenario in {"restart", "lease-loss"}
    run_cli(scheduler.execute, names, once)


if __name__ == "__main__":
    main()
