"""TOPIS target lookup, bounded collection, committed outcomes and cycle reports."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import CollectionRun
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.topis_rules import select_targets
from app.modules.data_pipeline.pipelines.topis_storage import (
    CYCLE_LOCK,
    checkpoint,
    consolidate,
    drain_file_deletions,
    recover_cycles,
    summary,
)
from app.repositories.collection import CollectionRepository
from app.repositories.reference import ReferenceRepository
from app.repositories.topis import CYCLE_TARGET, KEEP_CYCLES, SOURCE, prune_history

CONCURRENCY = 8
CHECKPOINT_SIZE = 50
CHECKPOINT_SECONDS = 1.0
REQUEST_INTERVAL = 0.05
RETRY_DELAYS = (0.5, 1.0)
RETRYABLE_ERRORS = {"provider_network_error", "provider_server_error"}
STOP_ERRORS = {"provider_quota_exceeded", "provider_auth_error", "missing_topis_api_key"}


async def targets(engine, max_links, scheduled_batch=False):
    async with async_sessionmaker(engine)() as session:
        repo = ReferenceRepository(session)
        links = await repo.topis_links()
        attempts = await repo.topis_attempt_times() if links and scheduled_batch else {}
    return select_targets(
        links,
        max_links,
        scheduled_batch=scheduled_batch,
        attempts=attempts,
    )


async def run_topis_network(engine, fetch_link, max_links, scheduled_batch=False):
    links, selection = await targets(engine, max_links, scheduled_batch)
    result = await run_topis(engine, fetch_link, links)
    return {**result, "selection": selection}


async def run_topis(engine, fetch_link, links):
    """One cycle log; bounded checkpoints preserve already committed successes on interruption."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    unique = list(dict.fromkeys(links))
    async with engine.connect() as lease:
        locked = await lease.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": CYCLE_LOCK}
        )
        await lease.commit()
        if not locked:
            return {"status": "skipped", "error_code": "already_running"}
        try:
            await recover_cycles(engine)
            async with factory.begin() as session:
                repo = CollectionRepository(session)
                run = await repo.start(SOURCE, CYCLE_TARGET)
                run_id = run.id
                await repo.archive(
                    run_id,
                    {"target": len(unique)},
                    None,
                    kind="derived_snapshot",
                    part="topis:start",
                )
                await prune_history(session)
            queue = asyncio.Queue()
            for link in unique:
                queue.put_nowait(link)
            entries, pending = [], []
            stop = asyncio.Event()
            pace, storage_lock = asyncio.Lock(), asyncio.Lock()
            checkpoint_index = 0

            async def flush():
                nonlocal checkpoint_index
                async with storage_lock:
                    while pending:
                        batch = pending[:CHECKPOINT_SIZE]
                        async with factory.begin() as session:
                            stored = await checkpoint(session, run_id, batch, checkpoint_index)
                        # Remove only after commit, so cancellation cannot lose buffered responses.
                        del pending[: len(batch)]
                        entries.extend(stored)
                        checkpoint_index += 1

            async def periodic_flush():
                while True:
                    await asyncio.sleep(CHECKPOINT_SECONDS)
                    await flush()

            async def worker():
                while not stop.is_set():
                    try:
                        link = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    data, error, attempts = None, None, 0
                    for attempt in range(len(RETRY_DELAYS) + 1):
                        if stop.is_set():
                            break
                        if attempt:
                            await asyncio.sleep(RETRY_DELAYS[attempt - 1])
                        async with pace:
                            await asyncio.sleep(REQUEST_INTERVAL)
                        if stop.is_set():
                            break
                        attempts += 1
                        try:
                            data = await fetch_link(link)
                            error = None
                            break
                        except CollectionError as exc:
                            error = str(exc)
                            if error in STOP_ERRORS:
                                stop.set()
                            if error not in RETRYABLE_ERRORS or attempt == len(RETRY_DELAYS):
                                break
                    if not attempts:
                        return
                    received_at = datetime.now(UTC)
                    async with storage_lock:
                        pending.append(
                            {
                                "link_id": link,
                                "data": data,
                                "error_code": error,
                                "received_at": received_at,
                                "attempts": attempts,
                            }
                        )
                        full = len(pending) >= CHECKPOINT_SIZE
                    if full:
                        await flush()

            interrupted = True
            try:
                async with asyncio.TaskGroup() as group:
                    timer = group.create_task(periodic_flush())
                    try:
                        async with asyncio.TaskGroup() as workers:
                            for _ in range(CONCURRENCY):
                                workers.create_task(worker())
                    finally:
                        timer.cancel()
                interrupted = False
            finally:
                # SIGKILL bypasses this block; the next source-lease owner consolidates checkpoints.
                await flush()
                result = summary(
                    entries, len(unique), interrupted=interrupted, stopped=stop.is_set()
                )
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    await consolidate(session, run, entries, result)
            result["run_id"] = str(run_id)
            result = await publish_result(engine, result, observations=("roads",))
            async with factory.begin() as session:
                retention = await prune_history(session)
            retention["files_removed"] = await drain_file_deletions(engine)
            result["retention"] = {"keep_cycles": KEEP_CYCLES, **retention}
            return result
        finally:
            await lease.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": CYCLE_LOCK}
            )
            await lease.commit()
