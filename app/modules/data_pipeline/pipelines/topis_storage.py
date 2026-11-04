"""Durable collection checkpoints, one consolidated cycle document and bounded cleanup."""

import asyncio
from collections import Counter
from datetime import datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    Base,
    CollectionRun,
    LegacyArchive,
    MapRoadObservation,
    PayloadFileDeletion,
    RawPayload,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.pipelines.snapshots import normalize_traffic
from app.modules.data_pipeline.pipelines.topis_rules import cycle_status
from app.repositories.collection import CollectionRepository, lock_payload_file
from app.repositories.map import lock_map_layer
from app.repositories.normalization import NormalizedWriter
from app.repositories.road_reference import lock_road_reference
from app.repositories.topis import (
    CHECKPOINT_PREFIX,
    CYCLE_PART,
    CYCLE_TARGET,
    SOURCE,
    prune_history,
    write_attempt,
)
from app.storage.payloads import configured_store

CYCLE_LOCK = "topis-cycle"


def summary(entries, target, *, interrupted=False, stopped=False):
    counts = Counter(e["error_code"] or e["status"] for e in entries)
    outcomes = Counter(e["status"] for e in entries)
    failed = [
        {
            "link_id": e["link_id"],
            "status": e["status"],
            "error_code": e["error_code"],
            "attempts": e["attempts"],
        }
        for e in entries
        if e["status"] != "success"
    ]
    status = cycle_status(counts, outcomes, stopped)
    if interrupted:
        status = (
            "partial" if any(e.get("processing", {}).get("updated") for e in entries) else "failed"
        )
    return {
        "status": status,
        "cycle_complete": not (interrupted or stopped),
        "target": target,
        "completed": len(entries),
        "remaining": target - len(entries),
        "counts": dict(counts),
        "retry_attempts": sum(e["attempts"] - 1 for e in entries),
        "failed_links": failed,
        **({"error_code": "collection_interrupted"} if interrupted else {}),
    }


async def checkpoint(session, run_id, entries, index):
    """One immutable file/transaction per batch; successes are durable before cycle completion."""
    repo = CollectionRepository(session)
    document = {
        "contract": "topis-checkpoint-v1",
        "source": SOURCE,
        "run_id": str(run_id),
        "entries": [{**e, "received_at": e["received_at"].isoformat()} for e in entries],
    }
    payload = await repo.archive(
        run_id,
        document,
        None,
        kind="derived_snapshot",
        part=CHECKPOINT_PREFIX + str(index),
    )
    processed = []
    for entry in entries:
        entry = dict(entry)
        if entry.get("data") is not None:
            try:
                async with session.begin_nested():
                    result = await session.run_sync(
                        lambda sync, entry=entry: normalize_traffic(
                            NormalizedWriter(sync, Base.metadata),
                            entry["link_id"],
                            entry["data"],
                            payload.payload_id,
                            entry["received_at"],
                        )
                    )
                entry.update(
                    status=result.status,
                    error_code=result.error_code,
                    processing={**result.counts.as_dict(), **result.details},
                )
            except Exception:
                entry.update(status="failed", error_code="normalization_failed", processing={})
        else:
            entry.update(status="failed", processing={})
        await write_attempt(session, run_id, entry)
        processed.append(entry)
    run = await session.get(CollectionRun, run_id)
    run.received += len(processed)
    run.accepted += sum(e.get("processing", {}).get("updated", False) for e in processed)
    run.rejected = run.received - run.accepted
    # Input is written before normalization; recovery uses the committed latest attempt
    # columns to reconstruct outcomes without creating per-road report files.
    return processed


async def consolidate(session, run, entries, result):
    """Replace temporary checkpoint references with a single cycle document atomically."""
    repo = CollectionRepository(session)
    payload = await repo.archive(
        run.id,
        {
            "contract": "topis-cycle-v1",
            "source": SOURCE,
            "run_id": str(run.id),
            "started_at": run.started_at.isoformat(),
            "summary": result,
            "entries": [{**e, "received_at": e["received_at"].isoformat()} for e in entries],
        },
        None,
        kind="derived_snapshot",
        part=CYCLE_PART,
    )
    chunks = select(RawPayload.payload_id).where(
        RawPayload.run_id == run.id,
        RawPayload.part_key.startswith(CHECKPOINT_PREFIX),
    )
    await session.run_sync(lock_road_reference)
    await session.run_sync(lambda sync: lock_map_layer(sync, "roads"))
    for model in (RoadTrafficLatest, MapRoadObservation):
        await session.execute(
            update(model).where(model.payload_id.in_(chunks)).values(payload_id=payload.payload_id)
        )
    accepted = sum(e.get("processing", {}).get("updated", False) for e in entries)
    await repo.finish(
        run,
        status="partial" if result["status"] == "paused_provider_limit" else result["status"],
        error_code=result.get("error_code"),
        completeness="complete" if result["cycle_complete"] else "partial",
        received=len(entries),
        accepted=accepted,
        rejected=len(entries) - accepted,
        rejection_reasons=dict(Counter(e["error_code"] for e in entries if e["error_code"])),
    )
    return payload


async def recover_cycles(engine):
    """Caller holds the source lease; no active collection is mistaken for an interrupted one."""
    from app.models import RoadTrafficAttemptLatest

    factory = async_sessionmaker(engine)
    async with factory() as session:
        pending = list(
            await session.scalars(
                select(CollectionRun.id).where(
                    CollectionRun.source == SOURCE,
                    CollectionRun.target_key == CYCLE_TARGET,
                    CollectionRun.status == "running",
                )
            )
        )
    for run_id in pending:
        async with factory.begin() as session:
            run = await session.get(CollectionRun, run_id)
            chunks = list(
                await session.scalars(
                    select(RawPayload)
                    .where(
                        RawPayload.run_id == run_id,
                        RawPayload.part_key.startswith(CHECKPOINT_PREFIX),
                    )
                    .order_by(RawPayload.stored_at, RawPayload.part_key)
                )
            )
            states = {
                s.link_id: s
                for s in await session.scalars(
                    select(RoadTrafficAttemptLatest).where(
                        RoadTrafficAttemptLatest.run_id == run_id
                    )
                )
            }
            entries = []
            for chunk in chunks:
                data = await asyncio.to_thread(configured_store().load_json, chunk)
                for entry in data["entries"]:
                    entry["received_at"] = datetime.fromisoformat(entry["received_at"])
                    state = states.get(entry["link_id"])
                    entry.update(
                        status=state.status if state else "partial",
                        error_code=state.error_code if state else "missing_road_geometry",
                        processing={"updated": state.observation_updated if state else False},
                    )
                    entries.append(entry)
            # Target is stored once in a start document and consolidated on interruption.
            initial = await session.scalar(
                select(RawPayload).where(
                    RawPayload.run_id == run_id,
                    RawPayload.part_key == "topis:start",
                )
            )
            target = (await asyncio.to_thread(configured_store().load_json, initial))["target"]
            await consolidate(session, run, entries, summary(entries, target, interrupted=True))


async def drain_file_deletions(engine):
    factory = async_sessionmaker(engine)
    removed = 0
    async with factory() as session:
        refs = list(await session.scalars(select(PayloadFileDeletion.file_ref)))
    for ref in refs:
        try:
            async with factory.begin() as session:
                await lock_payload_file(session, ref.rsplit("/", 1)[-1].removesuffix(".blob"))
                used = await session.scalar(
                    select(RawPayload.payload_id).where(RawPayload.file_ref == ref).limit(1)
                )
                archived = await session.scalar(
                    select(LegacyArchive.file_ref).where(LegacyArchive.file_ref == ref).limit(1)
                )
                if used is None and archived is None:
                    await asyncio.to_thread(configured_store().delete, ref)
                    removed += 1
                await session.execute(
                    delete(PayloadFileDeletion).where(PayloadFileDeletion.file_ref == ref)
                )
        except Exception:
            # The durable queue retains failed cleanup; the next cycle retries it.
            continue
    return removed


async def maintain_cycles(engine, *, recover=False):
    async with engine.connect() as lease:
        locked = await lease.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": CYCLE_LOCK}
        )
        await lease.commit()
        if not locked:
            return {"status": "skipped", "error_code": "already_running"}
        try:
            if recover:
                await recover_cycles(engine)
            async with async_sessionmaker(engine).begin() as session:
                result = await prune_history(session)
            result["files_removed"] = await drain_file_deletions(engine)
            return {"status": "success", **result}
        finally:
            await lease.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": CYCLE_LOCK}
            )
            await lease.commit()
