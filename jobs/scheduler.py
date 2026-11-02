"""Separate worker: bounded collection commands, monotonic scheduling and DB leases."""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.core.config import Settings
from app.db.session import create_engine


@dataclass(frozen=True)
class Task:
    module: str
    interval: int
    key_names: tuple[str, ...]
    args: tuple[str, ...] = ()
    timeout: int = 1800
    all_keys: bool = False
    prerequisites: tuple[str, ...] = ()
    failure_interval: int = 0


TASKS = {
    "citydata": Task("jobs.collect_citydata_all", 300, ("seoul_citydata_api_key",)),
    "parking": Task("jobs.collect_parking", 86400, ("seoul_api_key",)),
    "municipal": Task(
        "jobs.collect_municipal_parking", 300, ("seoul_municipal_parking_api_key", "seoul_api_key")
    ),
    "areas": Task(
        "jobs.collect_supplemental",
        604800,
        ("sgis_consumer_key", "sgis_consumer_secret"),
        ("areas",),
        all_keys=True,
    ),
    "sdot": Task(
        "jobs.collect_supplemental",
        600,
        ("seoul_sdot_api_key",),
        ("sdot",),
        prerequisites=("sgis_seoul_boundary",),
    ),
    "national-parking": Task(
        "jobs.collect_supplemental", 300, ("data_go_kr_api_key",), ("parking",)
    ),
    "cctv": Task(
        "jobs.collect_supplemental",
        3600,
        ("its_api_key",),
        ("cctv",),
        prerequisites=("sgis_seoul_boundary",),
    ),
    "topis": Task(
        "jobs.collect_topis_network",
        300,
        ("seoul_topis_api_key", "seoul_api_key"),
        ("--all", "--scheduled-batch", "--max-links", "200"),
        prerequisites=("topis_geometry:2025",),
        failure_interval=3600,
    ),
}


def report(task, status, **extra):
    print(
        json.dumps(
            {"time": datetime.now(UTC).isoformat(), "task": task, "status": status, **extra}
        ),
        flush=True,
    )


async def run_command(task):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        task.module,
        *task.args,
        # Collectors emit safe JSON on stdout. Do not expose arbitrary HTTP tracebacks.
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=task.timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def cycle(engine, name, task):
    async with engine.connect() as connection:
        for source in task.prerequisites:
            exists = await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM provider_snapshots WHERE source=:source)"),
                {"source": source},
            )
            if not exists:
                report(name, "waiting", error_code="missing_prerequisite", source=source)
                return None
        lease = "scheduler:" + name
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": lease}
        )
        await connection.commit()
        if not locked:
            report(name, "skipped", error_code="already_running")
            return True
        try:
            report(name, "running")
            code = await run_command(task)
            status = {0: "success", 2: "partial", 3: "skipped"}.get(code, "failed")
            report(name, status, exit_code=code)
            return code in {0, 2, 3}
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": lease}
            )
            await connection.commit()


async def worker(engine, name, task, once=False, sleep=asyncio.sleep, clock=None):
    now = clock or asyncio.get_running_loop().time
    while True:
        started = now()
        try:
            success = await cycle(engine, name, task)
        except Exception:
            # One provider/DB failure never terminates the other workers or future cycles.
            report(name, "failed", error_code="collection_or_database_error")
            success = False
        if once:
            return success
        interval = min(60, task.interval) if success is None else task.interval
        if success is False:
            interval = max(interval, task.failure_interval)
        await sleep(max(1, interval - (now() - started)))


async def main(names, once=False):
    settings = Settings()
    enabled = []
    for name in names:
        task = TASKS[name]
        configured = [bool(getattr(settings, field).get_secret_value()) for field in task.key_names]
        if not (all(configured) if task.all_keys else any(configured)):
            report(name, "skipped", error_code="missing_api_key")
        else:
            enabled.append((name, task))
    if not enabled:
        raise SystemExit("No configured collection tasks")
    engine = create_engine(settings)
    try:
        results = await asyncio.gather(
            *(worker(engine, name, task, once) for name, task in enabled)
        )
        if once and not all(results):
            raise SystemExit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main(list(dict.fromkeys(args.task or TASKS)), args.once))
    except KeyboardInterrupt:
        pass
