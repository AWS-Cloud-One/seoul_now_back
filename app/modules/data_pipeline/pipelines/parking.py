"""Preserve parking input, validate identities and atomically update typed registrations."""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import Base, CollectionRun
from app.modules.data_pipeline.collectors.parking import ParkingSnapshot
from app.modules.data_pipeline.identity import fingerprint
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
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.citydata import CityDataRepository
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.parking import ParkingRepository


@dataclass(frozen=True)
class ParkingInput:
    rows: list
    received_at: datetime | None
    parent_payload_id: object
    complete: bool = True
    error_code: str | None = None


async def archive_parking_selections(repo, run_id, candidates, duplicates):
    """Link every composite registration to its immutable inputs and field donors."""
    selected, merged_ids, input_rows = [], [], 0
    for candidate in candidates:
        if candidate.selection is None:
            selected.append(candidate)
            continue
        body = {
            **candidate.selection,
            "source": candidate.record["source"],
            "source_id": candidate.record["source_id"],
            "position": list(candidate.record["position"]),
            "selected_fields": candidate.record["input_fields"],
            "registration_received_at": candidate.received_at.isoformat()
            if candidate.received_at
            else None,
        }
        payload = await repo.archive(
            run_id,
            body,
            None,
            kind="derived_snapshot",
            part="merged:" + fingerprint([body["source"], body["source_id"], body["position"]]),
            legacy={"parent_payload_id": candidate.payload_id},
        )
        selected.append(replace(candidate, payload_id=payload.payload_id))
        merged_ids.append(str(payload.payload_id))
        input_rows += len(candidate.selection["inputs"])
    consolidated = input_rows - len(merged_ids)
    return selected, {
        "groups": len(merged_ids),
        "input_rows": input_rows,
        "consolidated_rows": consolidated,
        "exact_duplicates": duplicates - consolidated,
        "selected_payload_ids": merged_ids,
        "duplicates_scope": (
            "exact duplicates plus complementary rows consolidated into a selected record"
        ),
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
        async with factory.begin() as session:
            saved = await save_snapshot(
                session, MUNICIPAL_PARKING_SOURCE, {"rows": rows}, received_at=received_at
            )
            if paginated and saved.status == "success":
                # Completeness belongs to collection metadata, never to provider response bytes.
                run = await session.get(CollectionRun, saved.run_id)
                run.status = "success" if incoming.complete else "partial"
                run.completeness = "complete" if incoming.complete else "partial"
                run.error_code = incoming.error_code
        if saved.status not in {"success", "partial"}:
            raise CollectionError(saved.error_code or "incomplete_municipal_snapshot")
        return ParkingInput(
            attach_municipal_positions(rows, positions),
            received_at,
            saved.payload_id,
            incoming.complete if paginated else True,
            incoming.error_code if paginated else None,
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
                await CollectionRepository(session).start(
                    source, "all", run_id=run_id, started_at=started, kind=kind
                )
            status, error = "failed", None
            merge_summary = None
            try:
                incoming = await fetch()
                enriched = isinstance(incoming, ParkingInput)
                structured = isinstance(incoming, (ParkingInput, ParkingSnapshot))
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
                async with factory.begin() as session:
                    payload = await CollectionRepository(session).archive(
                        run_id,
                        rows,
                        None if enriched else received,
                        kind="provider_response"
                        if kind == "collection" and not enriched
                        else "derived_snapshot",
                        legacy={"parent_payload_id": incoming.parent_payload_id}
                        if enriched
                        else None,
                    )
                    payload_id = payload.payload_id
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
                    candidates.append(
                        ParkingCandidate(record, payload_id, received, source, row_index)
                    )
                if not candidates:
                    raise CollectionError("no_valid_records")
                conflicts = []
                selected, counters["duplicates"], rejected = select_parking_candidates(
                    candidates, conflicts=conflicts
                )
                reasons.update(rejected)
                counters["rejected"] += sum(rejected.values())
                async with factory.begin() as session:
                    selected, merge_summary = await archive_parking_selections(
                        CollectionRepository(session), run_id, selected, counters["duplicates"]
                    )
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


async def project_citydata_parking(engine, *, current_run_ids=None):
    async with async_sessionmaker(engine).begin() as session:
        # Serialize projections from the scheduler and manual reprocessing.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": CITYDATA_PARKING_SOURCE}
        )
        snapshots = await CityDataRepository(session).list_latest(
            section="parking", current_run_ids=current_run_ids
        )
        if not snapshots:
            return {"status": "skipped", "error_code": "no_citydata_payloads"}
        repo = CollectionRepository(session)
        run = await repo.start(CITYDATA_PARKING_SOURCE, "all", kind="reprocess")
        candidates = []
        reasons = Counter()
        section_inputs = [snapshot.evidence for snapshot in snapshots]
        section_issues = [
            item
            for item in section_inputs
            if item["latest_attempt"]["state"] not in {"usable", "empty"}
        ]
        received = duplicates = 0
        for snapshot in snapshots:
            if snapshot.payload_id is None:
                continue
            rows = snapshot.data["PRK_STTS"]
            payload = await repo.archive(
                run.id,
                rows,
                None,
                kind="derived_snapshot",
                part="area:" + snapshot.area_code,
                legacy={"parent_payload_id": snapshot.payload_id},
            )
            for row_index, row in enumerate(rows):
                received += 1
                try:
                    record = normalize_parking(row, snapshot.received_at, CITYDATA_PARKING_SOURCE)
                except ValueError, TypeError, KeyError, AttributeError:
                    reasons["invalid_parking_row"] += 1
                    continue
                candidates.append(
                    ParkingCandidate(
                        record,
                        payload.payload_id,
                        snapshot.received_at,
                        snapshot.area_code,
                        row_index,
                    )
                )
        conflicts = []
        selected, duplicates, rejected = select_parking_candidates(candidates, conflicts=conflicts)
        reasons.update(rejected)
        selected, merge_summary = await archive_parking_selections(
            repo, run.id, selected, duplicates
        )
        accepted = 0
        error = None
        try:
            # Preserve derived inputs and the run even if typed writes must roll back.
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
        await repo.finish(
            run,
            status="failed" if error else "partial" if reasons or section_issues else "success",
            error_code=error,
            **processing.database_counts(),
        )
        report = dict(
            run_id=str(run.id),
            status=run.status,
            error_code=error,
            source=CITYDATA_PARKING_SOURCE,
            **processing.as_dict(),
            merge_summary=merge_summary,
            section_inputs=section_inputs,
            section_issues=len(section_issues),
            writes_scope="parking source records; availability quality is stored separately",
        )
        await repo.archive(run.id, report, None, kind="processing_report", part="projection")
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
