"""Normalize current parking responses in memory and atomically store typed values."""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import Base, CollectionRun
from app.modules.data_pipeline.collectors.parking import ParkingSnapshot
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
    attach_municipal_positions,
    normalize_parking,
)
from app.modules.data_pipeline.normalizers.parking_resolution import (
    ParkingCandidate,
    select_parking_candidates,
)
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.parking import ParkingRepository


def parking_merge_summary(candidates, duplicates):
    merged = [candidate for candidate in candidates if candidate.selection is not None]
    input_rows = sum(len(candidate.selection["inputs"]) for candidate in merged)
    consolidated = input_rows - len(merged)
    return {
        "groups": len(merged),
        "input_rows": input_rows,
        "consolidated_rows": consolidated,
        "exact_duplicates": duplicates - consolidated,
    }


async def run_municipal_parking(engine, fetch):
    factory = async_sessionmaker(engine)

    async def fetch_with_positions():
        incoming = await fetch()
        paginated = isinstance(incoming, ParkingSnapshot)
        rows = incoming.rows if paginated else incoming
        received_at = incoming.received_at if paginated else datetime.now(UTC)
        async with factory() as session:
            positions = await ParkingRepository(session).active_positions(SOURCE)
        enriched = attach_municipal_positions(rows, positions)
        return (
            replace(incoming, rows=enriched)
            if paginated
            else ParkingSnapshot(enriched, len(rows), received_at, True)
        )

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
                repo = CollectionRepository(session)
                await repo.recover_interrupted(source)
                await repo.start(source, "all", run_id=run_id, started_at=started, kind=kind)
            status, error = "failed", None
            merge_summary = None
            try:
                incoming = await fetch()
                structured = isinstance(incoming, ParkingSnapshot)
                rows = incoming.rows if structured else incoming
                complete = incoming.complete if structured else True
                error = incoming.error_code if structured else None
                received = (
                    incoming.received_at
                    if structured
                    else datetime.now(UTC)
                    if kind == "collection"
                    else None
                )
                if not isinstance(rows, list) or not rows:
                    raise CollectionError("empty_or_invalid_snapshot")
                counters["received"] = len(rows)
                candidates = []
                for row_index, row in enumerate(rows):
                    try:
                        if not isinstance(row, dict):
                            raise ValueError("invalid_row")
                        record = normalize_parking(row, received, source)
                    except ValueError as exc:
                        counters["rejected"] += 1
                        reasons[str(exc)] += 1
                        continue
                    candidates.append(ParkingCandidate(record, received, source, row_index))
                if not candidates:
                    raise CollectionError("no_valid_records")
                conflicts = []
                selected, counters["duplicates"], rejected = select_parking_candidates(
                    candidates, conflicts=conflicts
                )
                reasons.update(rejected)
                counters["rejected"] += sum(rejected.values())
                merge_summary = parking_merge_summary(selected, counters["duplicates"])
                async with factory.begin() as session:
                    result = await session.run_sync(
                        lambda sync: NormalizedWriter(sync, Base.metadata).parking(
                            selected,
                            conflicts=conflicts,
                            complete=complete and not counters["rejected"] and kind == "collection",
                        )
                    )
                    counters["accepted"] = result["accepted"]
                    counters["rejected"] += result["held"]
                    reasons.update(result["rejection_reasons"])
                    status = "partial" if counters["rejected"] or not complete else "success"
                    run = await session.get(CollectionRun, run_id)
                    await CollectionRepository(session).finish(
                        run,
                        status=status,
                        error_code=error,
                        completeness=(
                            "partial"
                            if counters["rejected"] or not complete
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
                pending = counters["received"] - counters["duplicates"] - counters["rejected"]
                counters["accepted"] = 0
                counters["rejected"] += pending
                if pending:
                    reasons[error] += pending
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    await CollectionRepository(session).finish(
                        run,
                        status="failed",
                        error_code=error,
                        rejection_reasons=dict(reasons),
                        **counters,
                    )
            result = dict(
                run_id=str(run_id),
                source=source,
                status=status,
                error_code=error,
                **_processing_counts(counters, reasons).as_dict(),
                merge_summary=merge_summary,
            )
            result = await publish_result(engine, result, geometry=("parking",))
            if error in {"provider_auth_error", "provider_quota_exceeded"}:
                result["status"] = "paused_provider_limit"
            return result
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
            )
            await lock.commit()


@dataclass
class CityDataParkingBatch:
    """Only selected current-response candidates; never a saved/replayed snapshot."""

    candidates: list = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    received: int = 0
    section_issues: int = 0
    sections: int = 0

    def extend(self, other):
        self.candidates.extend(other.candidates)
        self.reasons.update(other.reasons)
        self.received += other.received
        self.section_issues += other.section_issues
        self.sections += other.sections


async def save_citydata_parking(engine, batch, *, run_id):
    if not batch.sections:
        return {"status": "skipped", "error_code": "no_current_citydata_sections"}
    async with async_sessionmaker(engine).begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": CITYDATA_PARKING_SOURCE}
        )
        repo = CollectionRepository(session)
        await repo.ensure_source(CITYDATA_PARKING_SOURCE)
        candidates, reasons = batch.candidates, batch.reasons.copy()
        received = batch.received
        conflicts = []
        selected, duplicates, rejected = select_parking_candidates(candidates, conflicts=conflicts)
        reasons.update(rejected)
        merge_summary = parking_merge_summary(selected, duplicates)
        accepted = 0
        error = None
        try:
            # Roll back typed changes together while retaining the short run outcome.
            async with session.begin_nested():
                result = await session.run_sync(
                    lambda sync: NormalizedWriter(sync, Base.metadata).parking(
                        selected, complete=False, conflicts=conflicts
                    )
                )
                accepted = result["accepted"]
            reasons.update(result["rejection_reasons"])
        except Exception:
            accepted = 0
            error = "normalization_failed"
            reasons[error] += len(selected)
        reasons = +reasons
        processing = _processing_counts(
            dict(
                received=received,
                accepted=accepted,
                rejected=sum(reasons.values()),
                duplicates=duplicates,
            ),
            reasons,
        )
        status = "failed" if error else "partial" if reasons or batch.section_issues else "success"
        await repo.add_counts(run_id, **processing.database_counts())
        report = dict(
            run_id=str(run_id),
            status=status,
            error_code=error,
            source=CITYDATA_PARKING_SOURCE,
            **processing.as_dict(),
            merge_summary=merge_summary,
            section_issues=batch.section_issues,
            writes_scope="parking source records; availability quality is stored separately",
        )
    return await publish_result(engine, report, geometry=("parking",))


def _processing_counts(counters, reasons):
    return ProcessingCounts(
        unit="row",
        scope="parking_normalization",
        **counters,
        conflicts=sum(
            reasons[key]
            for key in ("conflicting_parking_registration", "conflicting_parking_observation")
        ),
        rejection_reasons=dict(+reasons),
    )
