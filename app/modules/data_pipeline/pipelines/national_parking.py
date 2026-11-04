"""Archive each method/page before atomic, source-local parking normalization."""

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
from app.repositories.national_parking_progress import load_national_parking_progress
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
            pagination=dataset.get("pagination"),
            **counts.as_dict(),
        )
    return results


async def save_national_parking(session, data):
    # Serialize direct replays as well as the normal source-locked collector path.
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
            received = timestamp(page["received_at"]).value
            if received is None:
                raise ValueError("missing_page_receipt")
            payload = await repo.archive(
                run.id, page["body"], received, part=f"{method}:{page['page']}"
            )
            if page["usable"]:
                candidates.extend(
                    ParkingCandidate(row, payload.payload_id, received, method)
                    for row in page["body"][method]
                )
        datasets[method] = {**dataset, "candidates": candidates}
    error = None
    try:
        async with session.begin_nested():
            results = await session.run_sync(lambda sync: normalize_datasets(sync, datasets))
    except Exception:
        error = "normalization_failed"
        results = {
            method: dict(
                status="failed",
                collection_status=d["status"],
                complete=d["complete"],
                provider_total=d["provider_total"],
                error_code=error,
                limitations={},
                pagination=d.get("pagination"),
                **ProcessingCounts(
                    unit="row",
                    scope=method,
                    received=len(d["candidates"]),
                    rejected=len(d["candidates"]),
                    rejection_reasons={error: len(d["candidates"])} if d["candidates"] else {},
                ).as_dict(),
            )
            for method, d in datasets.items()
        }
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
    report = dict(
        source=normalizer.SOURCE,
        status=status,
        error_code=error,
        run_id=str(run.id),
        records=totals.received,
        datasets=results,
        **totals.as_dict(),
    )
    next_progress = dict(current_progress)
    progress_results = {}
    for method, dataset in datasets.items():
        pagination = dataset.get("pagination")
        if not pagination or not pagination["resumable"]:
            continue
        if error == "normalization_failed":
            progress_results[method] = "held_storage_failure"
        elif pagination["before"] != current_progress.get(method):
            # A repeated/stale batch must never rewind or skip the committed cursor.
            progress_results[method] = "held_stale_batch"
        else:
            next_progress[method] = pagination["after"]
            progress_results[method] = "recorded"
    # Keep state in the existing report, committed atomically with typed writes. Explicit
    # replay data without pagination still carries forward the previous checkpoint.
    report["pagination_progress"] = {
        "version": 1,
        "state": next_progress,
        "results": progress_results,
    }
    await repo.archive(run.id, report, None, kind="processing_report", part="report")
    await repo.finish(
        run,
        status=status,
        error_code=error,
        completeness="complete" if status == "success" else "partial",
        **totals.database_counts(),
    )
    return report
