"""Atomically store selected parking values and committed page progress, without archives."""

from collections import Counter
from dataclasses import replace

from sqlalchemy import text

from app.models import Base
from app.modules.data_pipeline.normalizers import national_parking as normalizer
from app.modules.data_pipeline.normalizers.parking_resolution import ParkingCandidate
from app.modules.data_pipeline.normalizers.values import timestamp
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.national_parking import NationalParkingWriter
from app.repositories.national_parking_progress import (
    load_national_parking_progress,
    save_national_parking_progress,
)
from app.repositories.normalization import NormalizedWriter


def normalize_datasets(sync, datasets):
    writer = NormalizedWriter(sync, Base.metadata)
    results, blocked = {}, set()
    for method in normalizer.METHODS:
        dataset = datasets[method]
        selected, duplicates, reasons, conflicts = normalizer.select_records(dataset["candidates"])
        accepted, limitations = 0, {}
        if method == "PrkSttusInfo":
            blocked.update(conflicts)
            candidates = []
            for item in selected:
                try:
                    candidates.append(replace(item, record=normalizer.facility(item.record)))
                except (ValueError, TypeError, KeyError) as exc:
                    code = str(exc) if isinstance(exc, ValueError) else "invalid_facility"
                    reasons[code] += 1
                    blocked.add(normalizer.management_id(item.record))
            # A bounded national response cannot close previously registered facilities.
            written = writer.parking(candidates, complete=False)
            accepted = written["accepted"]
            reasons.update(written["rejection_reasons"])
            blocked.update(
                c.record["source_id"]
                for c in candidates
                if c.record["identity_key"] in written["held_identity_keys"]
            )
        else:
            written = NationalParkingWriter(writer).attach(method, selected, blocked=blocked)
            accepted = written["accepted"]
            reasons.update(written["rejection_reasons"])
            limitations = written["limitations"]
        counts = ProcessingCounts(
            unit="row",
            scope=method,
            received=len(dataset["candidates"]),
            accepted=accepted,
            rejected=sum(reasons.values()),
            duplicates=duplicates,
            conflicts=reasons["conflicting_management_id"],
            rejection_reasons=dict(+reasons),
        )
        results[method] = dict(
            collection_status=dataset["status"],
            complete=dataset["complete"],
            provider_total=dataset["provider_total"],
            error_code=dataset["error_code"],
            status="failed"
            if dataset["status"] == "failed"
            else "partial"
            if not dataset["complete"] or reasons or limitations
            else "success",
            limitations=limitations,
            pagination=pagination_summary(dataset),
            **counts.as_dict(),
        )
    return results


def pagination_summary(dataset):
    pagination = dataset.get("pagination")
    return (
        {key: value for key, value in pagination.items() if key not in {"before", "after"}}
        if pagination
        else None
    )


def failed_results(datasets, error):
    return {
        method: dict(
            status="failed",
            collection_status=dataset["status"],
            complete=dataset["complete"],
            provider_total=dataset["provider_total"],
            error_code=error,
            limitations={},
            pagination=pagination_summary(dataset),
            **ProcessingCounts(
                unit="row",
                scope=method,
                received=len(dataset["candidates"]),
                rejected=len(dataset["candidates"]),
                rejection_reasons={error: len(dataset["candidates"])}
                if dataset["candidates"]
                else {},
            ).as_dict(),
        )
        for method, dataset in datasets.items()
    }


async def save_national_parking(session, data):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('national-parking-pagination'))")
    )
    current_progress = await load_national_parking_progress(session)
    repo = CollectionRepository(session)
    run = await repo.start(normalizer.SOURCE, "all")
    datasets = {}
    for method in normalizer.METHODS:
        dataset = data["datasets"][method]
        candidates = []
        for page in dataset["pages"]:
            if not page["usable"]:
                continue
            received = timestamp(page["received_at"]).value
            if received is None:
                raise ValueError("missing_page_receipt")
            candidates.extend(
                ParkingCandidate(row, received, f"{method}:page:{page['page']}", row_index)
                for row_index, row in enumerate(page["rows"])
            )
        datasets[method] = {**dataset, "candidates": candidates}
    next_progress = dict(current_progress)
    progress_results = {}
    stale = False
    for method, dataset in datasets.items():
        pagination = dataset.get("pagination")
        if not pagination or not pagination["resumable"]:
            continue
        stale |= pagination["before"] != current_progress.get(method)
        next_progress[method] = pagination["after"]
        progress_results[method] = "recorded"
    error = None
    if stale:
        # Reject the writes too: an old batch cannot update data while its cursor is held.
        error = "stale_parking_batch"
        results = failed_results(datasets, error)
        progress_results = dict.fromkeys(progress_results, "held_stale_batch")
    else:
        try:
            async with session.begin_nested():
                results = await session.run_sync(lambda sync: normalize_datasets(sync, datasets))
                await save_national_parking_progress(session, next_progress, current_progress)
        except Exception:
            error = "normalization_failed"
            results = failed_results(datasets, error)
            progress_results = dict.fromkeys(progress_results, "held_storage_failure")
    if error:
        next_progress = current_progress
    if all(item["status"] == "failed" for item in results.values()):
        error = error or "parking_all_methods_failed"
    status = (
        "failed"
        if error
        else "partial"
        if any(item["status"] != "success" for item in results.values())
        else "success"
    )
    reasons = Counter()
    for method, item in results.items():
        reasons.update(
            {method + ":" + key: value for key, value in item["rejection_reasons"].items()}
        )
    totals = ProcessingCounts(
        unit="source_row",
        scope="national_parking",
        **{
            key: sum(item[key] for item in results.values())
            for key in ("received", "accepted", "rejected", "duplicates", "conflicts")
        },
        rejection_reasons=dict(reasons),
    )
    await repo.finish(
        run,
        status=status,
        error_code=error,
        completeness="complete" if status == "success" else "partial",
        **totals.database_counts(),
    )
    return dict(
        source=normalizer.SOURCE,
        status=status,
        error_code=error,
        run_id=str(run.id),
        records=totals.received,
        datasets=results,
        **totals.as_dict(),
        pagination_progress={
            "results": progress_results,
            "next_pages": {method: state["next_page"] for method, state in next_progress.items()},
        },
    )
