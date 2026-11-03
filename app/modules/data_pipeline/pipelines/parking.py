"""Preserve parking input, validate identities and atomically update typed registrations."""

from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import Base, CollectionRun
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
    attach_municipal_positions,
    normalize_parking,
)
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.parking import ParkingRepository


async def run_municipal_parking(engine, fetch):
    factory = async_sessionmaker(engine)

    async def fetch_with_positions():
        rows = await fetch()
        async with factory() as session:
            positions = await ParkingRepository(session).active_positions(SOURCE)
        async with factory.begin() as session:
            saved = await save_snapshot(session, MUNICIPAL_PARKING_SOURCE, {"rows": rows})
        if saved.status != "success":
            raise CollectionError(saved.error_code or "incomplete_municipal_snapshot")
        return attach_municipal_positions(rows, positions)

    return await run_parking(engine, fetch_with_positions, MUNICIPAL_PARKING_SOURCE)


async def run_parking(engine, fetch, source, *, kind="collection"):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = datetime.now(UTC)
    counters = dict(received=0, accepted=0, rejected=0, duplicates=0)
    reasons = Counter()
    async with engine.connect() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:source))"), {"source": source}
        )
        await lock.commit()
        if not acquired:
            return {"status": "skipped", "error_code": "already_running"}
        try:
            async with factory.begin() as session:
                await CollectionRepository(session).start(
                    source, "all", run_id=run_id, started_at=started, kind=kind
                )
            status, error = "failed", None
            try:
                rows = await fetch()
                received = datetime.now(UTC) if kind == "collection" else None
                async with factory.begin() as session:
                    payload = await CollectionRepository(session).archive(
                        run_id,
                        rows,
                        received,
                        kind="provider_response" if kind == "collection" else "derived_snapshot",
                    )
                    payload_id = payload.payload_id
                if not isinstance(rows, list) or not rows:
                    raise CollectionError("empty_or_invalid_snapshot")
                counters["received"] = len(rows)
                normalized = {}
                for row in rows:
                    try:
                        if not isinstance(row, dict):
                            raise ValueError("invalid_row")
                        record = normalize_parking(row, received, source)
                    except ValueError as exc:
                        counters["rejected"] += 1
                        reasons[str(exc)] += 1
                        continue
                    if record["id"] in normalized:
                        if normalized[record["id"]] != record:
                            raise CollectionError("conflicting_duplicate")
                        counters["duplicates"] += 1
                    else:
                        normalized[record["id"]] = record
                if not normalized:
                    raise CollectionError("no_valid_records")
                async with factory.begin() as session:
                    result = await session.run_sync(
                        lambda sync: NormalizedWriter(sync, Base.metadata).parking(
                            list(normalized.values()),
                            payload_id,
                            received,
                            complete=not counters["rejected"] and kind == "collection",
                        )
                    )
                    counters["accepted"] = result["accepted"]
                    counters["rejected"] += result["held"]
                    if result["held"]:
                        reasons["identity_change_requires_review"] += result["held"]
                    status = "partial" if counters["rejected"] else "success"
                    run = await session.get(CollectionRun, run_id)
                    await CollectionRepository(session).finish(
                        run,
                        status=status,
                        completeness=(
                            "partial"
                            if counters["rejected"]
                            else "complete"
                            if kind == "collection"
                            else "unknown"
                        ),
                        rejection_reasons=dict(reasons),
                        **counters,
                    )
            except Exception as exc:
                status = "failed"
                error = str(exc) if isinstance(exc, CollectionError) else "internal_error"
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    await CollectionRepository(session).finish(
                        run,
                        status="failed",
                        error_code=error,
                        rejection_reasons=dict(reasons),
                        **counters,
                    )
            return dict(
                run_id=str(run_id),
                source=source,
                status=status,
                error_code=error,
                rejection_reasons=dict(reasons),
                **counters,
            )
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
            )
            await lock.commit()


async def project_citydata_parking(engine):
    async with async_sessionmaker(engine).begin() as session:
        # Serialize projections from the scheduler and manual reprocessing.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": CITYDATA_PARKING_SOURCE}
        )
        snapshots = await CityDataRepository(session).list_latest()
        if not snapshots:
            return {"status": "skipped", "error_code": "no_citydata_payloads"}
        repo = CollectionRepository(session)
        run = await repo.start(CITYDATA_PARKING_SOURCE, "all", kind="reprocess")
        selected = {}
        reasons = Counter()
        received = duplicates = 0
        payloads = {}
        for snapshot in snapshots:
            rows = snapshot.data.get("PRK_STTS") or []
            payload = await repo.archive(
                run.id,
                rows,
                None,
                kind="derived_snapshot",
                part="area:" + snapshot.area_code,
                legacy={"parent_payload_id": snapshot.payload_id},
            )
            payloads[snapshot.area_code] = payload.payload_id
            for row in rows:
                received += 1
                try:
                    record = normalize_parking(row, snapshot.received_at, CITYDATA_PARKING_SOURCE)
                except ValueError, TypeError, KeyError, AttributeError:
                    reasons["invalid_parking_row"] += 1
                    continue
                when = record["source_updated_at"] or datetime.min.replace(tzinfo=UTC)
                prior = selected.get(record["id"])
                if prior:
                    duplicates += 1
                if prior is None or when > prior[0]:
                    selected[record["id"]] = (when, record, snapshot)
        accepted = held = 0
        error = None
        try:
            # Preserve derived inputs and the run even if typed writes must roll back.
            async with session.begin_nested():
                for snapshot in snapshots:
                    rows = [
                        record
                        for _, record, origin in selected.values()
                        if origin.area_code == snapshot.area_code
                    ]
                    result = await session.run_sync(
                        lambda sync, rows=rows, snapshot=snapshot: NormalizedWriter(
                            sync, Base.metadata
                        ).parking(
                            rows, payloads[snapshot.area_code], snapshot.received_at, complete=False
                        )
                    )
                    accepted += result["accepted"]
                    held += result["held"]
            reasons["identity_change_requires_review"] += held
        except Exception:
            accepted = 0
            error = "normalization_failed"
        reasons = +reasons
        await repo.finish(
            run,
            status="failed" if error else "partial" if reasons else "success",
            error_code=error,
            received=received,
            accepted=accepted,
            rejected=sum(reasons.values()),
            duplicates=duplicates,
            rejection_reasons=dict(reasons),
        )
        return dict(
            run_id=str(run.id),
            status=run.status,
            error_code=error,
            source=CITYDATA_PARKING_SOURCE,
            received=received,
            accepted=accepted,
            rejected=run.rejected,
            duplicates=duplicates,
            rejection_reasons=dict(reasons),
        )
