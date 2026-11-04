"""TOPIS target lookup, bounded collection, committed outcomes and cycle reports."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.normalizers.topis import traffic_observation
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.topis_rules import select_targets
from app.modules.data_pipeline.pipelines.topis_storage import (
    CYCLE_LOCK,
    finish_cycle,
    recover_cycles,
    store_batch,
    summary,
)
from app.repositories.collection import CollectionRepository
from app.repositories.reference import ReferenceRepository
from app.repositories.topis import CYCLE_TARGET, KEEP_CYCLES, SOURCE, prune_history

CONCURRENCY = 8
BATCH_SIZE = 50
BATCH_SECONDS = 1.0
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
    """One cycle log; committed typed batches survive interruption without replay files."""
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
                await prune_history(session)
            queue = asyncio.Queue()
            for link in unique:
                queue.put_nowait(link)
            entries, pending = [], []
            stop = asyncio.Event()
            pace, storage_lock = asyncio.Lock(), asyncio.Lock()
            workers_finished = asyncio.Event()

            async def flush():
                async with storage_lock:
                    while pending:
                        batch = pending[:BATCH_SIZE]
                        async with factory.begin() as session:
                            stored = await store_batch(session, run_id, batch)
                        # Retain only compact outcomes after committing the typed values.
                        del pending[: len(batch)]
                        entries.extend(stored)

            async def periodic_flush():
                while not workers_finished.is_set():
                    try:
                        await asyncio.wait_for(workers_finished.wait(), BATCH_SECONDS)
                    except TimeoutError:
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
                    attempted_at = datetime.now(UTC)
                    received_at = (
                        data.get("received_at", attempted_at) if isinstance(data, dict) else None
                    )
                    entry = (
                        traffic_observation(link, data, received_at)
                        if error is None
                        else dict(values=None, status="failed", error_code=error, processing={})
                    )
                    # Full provider responses never enter the buffer or the cycle report.
                    data = None
                    entry.update(link_id=link, attempted_at=attempted_at, attempts=attempts)
                    async with storage_lock:
                        pending.append(entry)
                        full = len(pending) >= BATCH_SIZE
                    if full:
                        await flush()

            completed = False
            pruned = 0
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(periodic_flush())
                    try:
                        async with asyncio.TaskGroup() as workers:
                            for _ in range(CONCURRENCY):
                                workers.create_task(worker())
                    finally:
                        workers_finished.set()
                await flush()
                result = summary(entries, len(unique), stopped=stop.is_set())
                async with factory.begin() as session:
                    pruned = await finish_cycle(session, run_id, result) or 0
                completed = True
            finally:
                if not completed:
                    # Discard uncommitted responses. DB counts are authoritative even if a
                    # cancellation arrived during COMMIT; never replay a possibly committed batch.
                    await recover_cycles(engine)
                async with factory.begin() as session:
                    retention = await prune_history(session)
                retention["runs_removed"] += pruned
            result["run_id"] = str(run_id)
            result = await publish_result(engine, result, observations=("roads",))
            result["retention"] = {"keep_cycles": KEEP_CYCLES, **retention}
            return result
        finally:
            await lease.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": CYCLE_LOCK}
            )
            await lease.commit()
