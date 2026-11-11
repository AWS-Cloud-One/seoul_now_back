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
from app.modules.data_pipeline.pipelines.maintenance import recover_collection
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.topis_storage import maintain_cycles
from app.repositories.reference import ReferenceRepository
from jobs.runtime import report as report_result
from jobs.runtime import run_cli
from jobs.shutdown import STATE, check_stopping, complete_cleanup, stop_child

LEASE_CHECK_SECONDS = 2
LEASE_TIMEOUT_SECONDS = 3


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
    "citydata": Task(
        "jobs.collect_citydata_all",
        600,
        ("seoul_citydata_api_key",),
        prerequisites=("citydata_places",),
    ),
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
        prerequisites=("sgis_seoul_boundary", "sdot_locations"),
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

INTERRUPTED_COLLECTIONS = {
    "citydata": "seoul_citydata",
    "parking": "seoul_getparkinfo",
    "municipal": "seoul_municipal_parking",
}


def report(task, status, **extra):
    report_result({"time": datetime.now(UTC).isoformat(), "task": task, "status": status, **extra})


async def run_command(task):
    check_stopping()
    # Shield creation so cancellation cannot lose a child between fork and registration.
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            task.module,
            *task.args,
            start_new_session=True,
            # Collectors emit safe JSON on stdout. Do not expose arbitrary HTTP tracebacks.
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    process = None
    state = STATE.get()
    try:
        process = await asyncio.shield(spawn)
        if state is not None:
            with state.lock:
                state.children.add(process.pid)
        return await asyncio.wait_for(process.wait(), timeout=task.timeout)
    finally:

        async def cleanup():
            child = process if process is not None else await spawn
            if state is not None:
                with state.lock:
                    state.children.add(child.pid)
            try:
                await stop_child(child)
            finally:
                if state is not None:
                    with state.lock:
                        state.children.discard(child.pid)

        await complete_cleanup(cleanup())


async def recover_publication(engine, name):
    """Recover committed rows independently of the collector process and its next API call."""
    layers = PUBLICATION_RECOVERY.get(name)
    if layers is None:
        return
    geometry, observations = layers
    for attempt in range(2):
        check_stopping()
        try:
            async with asyncio.timeout(120):
                if name == "topis":
                    await maintain_cycles(engine, recover=True)
                elif name in INTERRUPTED_COLLECTIONS:
                    await recover_collection(engine, INTERRUPTED_COLLECTIONS[name])
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


class LeaseLostError(Exception):
    pass


async def monitor_lease(connection, lease, pid):
    while True:
        await asyncio.sleep(LEASE_CHECK_SECONDS)
        try:
            async with asyncio.timeout(LEASE_TIMEOUT_SECONDS):
                owned = await connection.scalar(
                    text("""
                    SELECT pg_backend_pid() = :pid AND EXISTS (
                        SELECT 1 FROM pg_locks
                        WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
                          AND database = (
                              SELECT oid FROM pg_database WHERE datname = current_database()
                          )
                          AND classid = ((hashtext(:name)::bigint >> 32) & 4294967295)::oid
                          AND objid = (hashtext(:name)::bigint & 4294967295)::oid
                          AND objsubid = 1
                    )
                """),
                    {"name": lease, "pid": pid},
                )
                await connection.commit()
                if not owned:
                    raise LeaseLostError
        except Exception:
            raise LeaseLostError("scheduler_lease_lost") from None


async def run_cycle(engine, name, task):
    try:
        # Also runs after a scheduler restart: a killed parent cannot execute cleanup.
        await recover_publication(engine, name)
        check_stopping()
        report(name, "running")
        try:
            code = await run_command(task)
        except Exception:
            await recover_publication(engine, name)
            raise
        if code not in {0, 3}:
            await recover_publication(engine, name)
        status = {0: "success", 2: "partial", 3: "skipped", 4: "paused_provider_limit"}.get(
            code, "failed"
        )
        report(name, status, exit_code=code)
        return TaskResult(status, code)
    except asyncio.CancelledError:
        report(name, "publication_deferred", error_code="cycle_interrupted")
        raise


async def run_with_lease(connection, engine, name, task, lease, pid):
    work = asyncio.create_task(run_cycle(engine, name, task))
    monitor = asyncio.create_task(monitor_lease(connection, lease, pid))
    try:
        done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            # The child is stopped/reaped in finally before the lease connection is released.
            await monitor
        return await work
    finally:
        for operation in (work, monitor):
            if not operation.done():
                operation.cancel()
        await complete_cleanup(asyncio.gather(work, monitor, return_exceptions=True))


async def cycle(engine, name, task):
    check_stopping()
    async with engine.connect() as connection:
        for source in task.prerequisites:
            async with AsyncSession(bind=connection) as session:
                exists = await ReferenceRepository(session).has_prerequisite(source)
            if not exists:
                report(name, "waiting", error_code="missing_prerequisite", source=source)
                return TaskResult("waiting", 1, "missing_prerequisite")
        lease = "scheduler:" + name
        try:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": lease}
            )
            await connection.commit()
        except BaseException:
            # Cancellation can arrive after the server acquired a session lock but
            # before Python received the result. Never return that session to the pool.
            await complete_cleanup(connection.invalidate())
            raise
        if not locked:
            report(name, "skipped", error_code="already_running")
            return TaskResult("skipped", 3, "already_running")
        try:
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            return await run_with_lease(connection, engine, name, task, lease, pid)
        finally:

            async def release():
                try:
                    async with asyncio.timeout(LEASE_TIMEOUT_SECONDS):
                        await connection.rollback()
                        if not connection.invalidated:
                            await connection.execute(
                                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": lease}
                            )
                            await connection.commit()
                            report(name, "lease_released")
                except Exception:
                    await connection.invalidate()
                    report(name, "lease_disconnected")

            await complete_cleanup(release())


async def worker(
    engine, name, task, once=False, sleep=asyncio.sleep, clock=None, first_cycle_done=None
):
    now = clock or asyncio.get_running_loop().time
    while True:
        check_stopping()
        started = now()
        try:
            result = await cycle(engine, name, task)
        except TimeoutError:
            report(name, "failed", error_code="collection_timeout")
            result = TaskResult("failed", 1, "collection_timeout")
        except LeaseLostError:
            report(name, "failed", error_code="scheduler_lease_lost")
            result = TaskResult("failed", 1, "scheduler_lease_lost")
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

        workers = [asyncio.create_task(selected_worker(name, task)) for name, task in enabled]
        try:
            completed = await asyncio.gather(*workers)
        finally:
            for running in workers:
                if not running.done() and not running.cancelling():
                    running.cancel()
            await complete_cleanup(asyncio.gather(*workers, return_exceptions=True))
        results.update({name: result for (name, _), result in zip(enabled, completed, strict=True)})
        return report_once(results) if once else 0
    finally:
        await complete_cleanup(engine.dispose())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_cli(execute, list(dict.fromkeys(args.task or TASKS)), args.once)


if __name__ == "__main__":
    main()
