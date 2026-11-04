"""Separate worker: bounded collection commands, monotonic scheduling and DB leases."""

import argparse
import asyncio
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.topis_storage import maintain_cycles
from app.repositories.reference import ReferenceRepository
from jobs.runtime import report as report_result
from jobs.runtime import run_cli


@dataclass(frozen=True)
class Task:
    module: str
    interval: int
    key_names: tuple[str, ...]
    args: tuple[str, ...] = ()
    timeout: int = 1800
    all_keys: bool = False
    prerequisites: tuple[str, ...] = ()
    failure_interval: int = 300
    limit_interval: int = 3600
    after: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskResult:
    status: str
    exit_code: int
    error_code: str | None = None


TASKS = {
    "citydata": Task("jobs.collect_citydata_all", 600, ("seoul_citydata_api_key",)),
    "parking": Task("jobs.collect_parking", 86400, ("seoul_api_key",)),
    "municipal": Task(
        "jobs.collect_municipal_parking",
        600,
        ("seoul_municipal_parking_api_key", "seoul_api_key"),
        after=("parking",),
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
        failure_interval=600,
        after=("areas",),
    ),
    "national-parking": Task(
        "jobs.collect_supplemental",
        3600,
        ("data_go_kr_api_key",),
        ("parking",),
        failure_interval=3600,
    ),
    "topis": Task(
        "jobs.collect_topis_network",
        600,
        ("seoul_topis_api_key", "seoul_api_key"),
        ("--all",),
        prerequisites=("topis_geometry",),
        failure_interval=3600,
    ),
}

PUBLICATION_RECOVERY = {
    "topis": ((), ("roads",)),
    "citydata": (("parking",), ("population",)),
    "parking": (("parking",), ()),
    "municipal": (("parking",), ()),
    "national-parking": (("parking",), ()),
}


def report(task, status, **extra):
    report_result({"time": datetime.now(UTC).isoformat(), "task": task, "status": status, **extra})


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


async def recover_publication(engine, name):
    """Recover committed rows independently of the collector process and its next API call."""
    layers = PUBLICATION_RECOVERY.get(name)
    if layers is None:
        return
    geometry, observations = layers
    for attempt in range(2):
        try:
            async with asyncio.timeout(120):
                if name == "topis":
                    await maintain_cycles(engine, recover=True)
                result = await publish_result(
                    engine, {"status": "success"}, geometry=geometry, observations=observations
                )
            publication = result["publication"]
        except Exception:
            publication = {"status": "failed", "error_code": "map_publication_failed"}
        report(name, "publication_" + publication["status"], publication=publication)
        if publication["status"] != "failed":
            return publication
        if attempt == 0:
            await asyncio.sleep(1)
    return publication


async def cycle(engine, name, task):
    async with engine.connect() as connection:
        for source in task.prerequisites:
            async with AsyncSession(bind=connection) as session:
                exists = await ReferenceRepository(session).has_prerequisite(source)
            if not exists:
                report(name, "waiting", error_code="missing_prerequisite", source=source)
                return TaskResult("waiting", 1, "missing_prerequisite")
        lease = "scheduler:" + name
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": lease}
        )
        await connection.commit()
        if not locked:
            report(name, "skipped", error_code="already_running")
            return TaskResult("skipped", 3, "already_running")
        try:
            # Also runs after a scheduler restart: a killed parent cannot execute cleanup.
            await recover_publication(engine, name)
            report(name, "running")
            code = None
            try:
                code = await run_command(task)
            finally:
                # run_command has reaped the child before we read its committed successes.
                # Keep the scheduler lease until recovery finishes.
                if code not in {0, 3}:
                    await recover_publication(engine, name)
            status = {0: "success", 2: "partial", 3: "skipped", 4: "paused_provider_limit"}.get(
                code, "failed"
            )
            report(name, status, exit_code=code)
            return TaskResult(status, code)
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": lease}
            )
            await connection.commit()


async def worker(
    engine, name, task, once=False, sleep=asyncio.sleep, clock=None, first_cycle_done=None
):
    now = clock or asyncio.get_running_loop().time
    while True:
        started = now()
        try:
            result = await cycle(engine, name, task)
        except TimeoutError:
            report(name, "failed", error_code="collection_timeout")
            result = TaskResult("failed", 1, "collection_timeout")
        except Exception:
            # One provider/DB failure never terminates the other workers or future cycles.
            report(name, "failed", error_code="collection_or_database_error")
            result = TaskResult("failed", 1, "collection_or_database_error")
        if first_cycle_done is not None:
            first_cycle_done.set()
        if once:
            return result
        # Successful/partial cycles retain start-based cadence. A failed attempt gets a
        # separate delay after completion, independent of infrequent reference refreshes.
        if result.status == "paused_provider_limit":
            delay = max(task.failure_interval, task.limit_interval)
        elif result.status == "failed":
            delay = task.failure_interval
        elif result.status == "waiting":
            delay = min(60, task.interval)
        else:
            delay = max(1, task.interval - (now() - started))
        await sleep(max(1, delay))


def aggregate(results):
    statuses = {result.status for result in results}
    if "failed" in statuses:
        return TaskResult("failed", 1)
    if "paused_provider_limit" in statuses:
        return TaskResult("paused_provider_limit", 4)
    if "waiting" in statuses:
        return TaskResult("waiting", 1)
    if statuses == {"success"}:
        return TaskResult("success", 0)
    if not statuses or statuses == {"skipped"}:
        return TaskResult("skipped", 3)
    return TaskResult("partial", 2)


def report_once(results):
    outcome = aggregate(results.values())
    report(
        "scheduler",
        outcome.status,
        exit_code=outcome.exit_code,
        results={name: asdict(result) for name, result in results.items()},
    )
    return outcome.exit_code


async def execute(names, once=False) -> int:
    settings = Settings()
    enabled, results = [], {}
    for name in dict.fromkeys(names):
        task = TASKS[name]
        configured = [bool(getattr(settings, field).get_secret_value()) for field in task.key_names]
        if not (all(configured) if task.all_keys else any(configured)):
            report(name, "skipped", error_code="missing_api_key")
            results[name] = TaskResult("skipped", 3, "missing_api_key")
        else:
            enabled.append((name, task))
    if not enabled:
        return report_once(results) if once else 1
    engine = create_engine(settings)
    try:
        first_cycles = {name: asyncio.Event() for name, _ in enabled}

        async def selected_worker(name, task):
            # Wait only for selected and configured predecessors. Existing DB references
            # remain usable after a failed/partial refresh; cycle checks their actual presence.
            for dependency in task.after:
                if dependency in first_cycles:
                    await first_cycles[dependency].wait()
            return await worker(engine, name, task, once, first_cycle_done=first_cycles[name])

        completed = await asyncio.gather(*(selected_worker(name, task) for name, task in enabled))
        results.update({name: result for (name, _), result in zip(enabled, completed, strict=True)})
        return report_once(results) if once else 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_cli(execute, list(dict.fromkeys(args.task or TASKS)), args.once)


if __name__ == "__main__":
    main()
