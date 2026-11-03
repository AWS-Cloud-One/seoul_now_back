"""TOPIS target lookup, bounded collection, committed outcomes and cycle reports."""

import asyncio
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.modules.data_pipeline.pipelines.topis_rules import cycle_status, select_targets
from app.repositories.reference import ReferenceRepository

CONCURRENCY = 8
REQUEST_INTERVAL = 0.05
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


async def run_topis_network(engine, fetch_link, max_links, scheduled_batch=False, *, report=None):
    links, selection = await targets(engine, max_links, scheduled_batch)
    if report:
        report(selection)
    return await run_topis(engine, fetch_link, links, report=report)


async def run_topis(engine, fetch_link, links, *, report=None):
    """Fetch outside write transactions; callers own the engine and HTTP client."""
    factory = async_sessionmaker(engine)
    queue = asyncio.Queue()
    unique = list(dict.fromkeys(links))
    for link in unique:
        queue.put_nowait(link)
    counts, outcomes = Counter(), Counter()
    stop = asyncio.Event()
    pace, progress_lock = asyncio.Lock(), asyncio.Lock()
    started = datetime.now(UTC).isoformat()

    async def progress(status):
        async with progress_lock:
            async with factory.begin() as session:
                result = await save_snapshot(
                    session,
                    "topis_collection_status",
                    {
                        "status": status,
                        "started_at": started,
                        "target": len(unique),
                        "completed": sum(counts.values()),
                        "success": counts["success"],
                        "partial": outcomes["partial"],
                        "no_observation": counts["provider_no_observation"],
                        "failed": outcomes["failed"] - counts["provider_no_observation"],
                        "error_counts": dict(counts),
                        "remaining": len(unique) - sum(counts.values()),
                    },
                )
            if result.status != "success":
                raise CollectionError(result.error_code or "collection_report_failed")

    async def worker():
        while not stop.is_set():
            try:
                link = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with pace:
                await asyncio.sleep(REQUEST_INTERVAL)
            if stop.is_set():
                return
            try:
                data = await fetch_link(link)
                async with factory.begin() as session:
                    result = await save_snapshot(session, "topis:" + link, data)
                outcome, error = result.status, result.error_code
                status = error or outcome
            except CollectionError as exc:
                status = str(exc)
                outcome, error = "failed", status
                # Stop new dispatches as soon as a limit/auth failure is known.
                if status in STOP_ERRORS:
                    stop.set()
                async with factory.begin() as session:
                    await save_snapshot(
                        session,
                        "topis_attempt:" + link,
                        {"status": "failed", "error_code": status},
                    )
            counts[status] += 1
            outcomes[outcome] += 1
            if report:
                report({"link_id": link, "status": outcome, "error_code": error})
            if sum(counts.values()) % 50 == 0:
                await progress("running")

    await progress("running")
    # Unexpected worker failures cancel/drain siblings before callers close resources.
    async with asyncio.TaskGroup() as group:
        for _ in range(CONCURRENCY):
            group.create_task(worker())
    await progress("paused_provider_limit" if stop.is_set() else "complete")
    return {
        "cycle_complete": not stop.is_set(),
        "target": len(unique),
        "counts": dict(counts),
        "status": cycle_status(counts, outcomes, stop.is_set()),
    }
