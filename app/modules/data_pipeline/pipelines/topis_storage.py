"""Short atomic TOPIS batches and bounded run summaries; no response/checkpoint files."""

from collections import Counter

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import CollectionRun
from app.modules.data_pipeline.pipelines.topis_rules import cycle_status
from app.repositories.collection import CollectionRepository
from app.repositories.topis import (
    CYCLE_TARGET,
    SOURCE,
    has_active_road,
    prune_history,
    record_batch_counts,
    write_attempt,
    write_observation,
)

CYCLE_LOCK = "topis-cycle"


def summary(entries, target, *, interrupted=False, stopped=False):
    counts = Counter(e["error_code"] or e["status"] for e in entries)
    outcomes = Counter(e["status"] for e in entries)
    status = cycle_status(counts, outcomes, stopped)
    if interrupted:
        status = "partial" if any(e["processing"]["updated"] for e in entries) else "failed"
    return {
        "status": status,
        "cycle_complete": not (interrupted or stopped),
        "target": target,
        "completed": len(entries),
        "remaining": target - len(entries),
        "counts": dict(counts),
        "retry_attempts": sum(e["attempts"] - 1 for e in entries),
        "failed_links": [
            {key: e[key] for key in ("link_id", "status", "error_code", "attempts")}
            for e in entries
            if e["status"] != "success"
        ],
        **({"error_code": "collection_interrupted"} if interrupted else {}),
    }


async def store_batch(session, run_id, entries):
    """Only selected/validated fields enter the batch; discard them after a successful commit."""
    processed = []
    for entry in entries:
        outcome = {
            key: entry[key]
            for key in (
                "link_id",
                "status",
                "error_code",
                "attempted_at",
                "attempts",
            )
        }
        outcome["processing"] = {**entry["processing"], "updated": False}
        try:
            async with session.begin_nested():
                if entry["values"] is not None:
                    if not await has_active_road(session, entry["link_id"]):
                        outcome.update(status="partial", error_code="missing_road_geometry")
                    else:
                        updated = await session.run_sync(
                            lambda sync, entry=entry: write_observation(
                                sync, entry["link_id"], entry["values"]
                            )
                        )
                        outcome["processing"]["updated"] = updated
                        if not updated:
                            outcome.update(status="partial", error_code="traffic_time_guard")
                await write_attempt(session, run_id, outcome)
        except Exception:
            # Observation and attempt must not disagree if either write failed.
            outcome.update(status="failed", error_code="normalization_failed")
            outcome["processing"]["updated"] = False
            await write_attempt(session, run_id, outcome)
        processed.append(outcome)
    await record_batch_counts(session, run_id, processed)
    return processed


async def finish_cycle(session, run_id, result):
    repo = CollectionRepository(session)
    run = await session.get(CollectionRun, run_id)
    reasons = (await repo.rejection_counts([run_id]))[run_id]
    return await repo.finish(
        run,
        status="partial" if result["status"] == "paused_provider_limit" else result["status"],
        error_code=result.get("error_code")
        or (
            next(
                (
                    code
                    for code in reasons
                    if code
                    in {"provider_auth_error", "provider_quota_exceeded", "missing_topis_api_key"}
                ),
                None,
            )
            if result["status"] == "paused_provider_limit"
            else None
        ),
        completeness="complete" if result["cycle_complete"] else "partial",
        received=run.received,
        accepted=run.accepted,
        rejected=run.rejected,
        rejection_reasons=reasons,
    )


async def recover_cycles(engine):
    """Close abandoned runs from committed counts under the source lease, without replay."""
    async with async_sessionmaker(engine).begin() as session:
        runs = list(
            await session.scalars(
                select(CollectionRun)
                .where(
                    CollectionRun.source == SOURCE,
                    CollectionRun.target_key == CYCLE_TARGET,
                    CollectionRun.status == "running",
                )
                .with_for_update()
            )
        )
        for run in runs:
            await finish_cycle(
                session,
                run.id,
                {
                    "status": "partial" if run.accepted else "failed",
                    "error_code": "collection_interrupted",
                    "cycle_complete": False,
                },
            )
    return len(runs)


async def maintain_cycles(engine, *, recover=False):
    async with engine.connect() as lease:
        locked = await lease.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": CYCLE_LOCK}
        )
        await lease.commit()
        if not locked:
            return {"status": "skipped", "error_code": "already_running"}
        try:
            recovered = await recover_cycles(engine) if recover else 0
            async with async_sessionmaker(engine).begin() as session:
                result = await prune_history(session)
            return {"status": "success", "runs_recovered": recovered, **result}
        finally:
            await lease.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": CYCLE_LOCK}
            )
            await lease.commit()
