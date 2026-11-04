"""Archive sensor file/pages, then atomically store the verified reference and latest state."""

from app.modules.data_pipeline.normalizers.sdot import (
    OBSERVATION_FIELDS,
    observation_summary,
    sdot_snapshot,
    sensor_locations,
)
from app.modules.data_pipeline.normalizers.values import timestamp
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.reference import ReferenceRepository
from app.repositories.sensors import SensorWriter
from app.repositories.spatial import SpatialRepository


async def save_sdot(session, file, collected, *, started_at=None):
    repo = CollectionRepository(session)
    run = await repo.start("sdot_people", "all", started_at=started_at)
    original = await repo.archive_file(
        run.id, file["body"], part="installation", metadata={**file, "version": file["sha256"]}
    )
    page_ids = {}
    if collected is not None:
        for index, page in enumerate(collected["pages"]):
            if "body" not in page:
                continue
            raw = await repo.archive(
                run.id, page["body"], timestamp(page.get("received_at")).value, part=f"page:{index}"
            )
            page_ids[index] = str(raw.payload_id)
    data, result, error, derived = None, None, None, None
    try:
        reference = sensor_locations(file)
        if collected is None:
            raise ValueError("missing_sdot_collection")
        data = sdot_snapshot(reference, collected)
        data["reference"]["payload_id"] = str(original.payload_id)
        for index, page in enumerate(data["pages"]):
            page["payload_id"] = page_ids.get(index)
        for row in data["input_rows"]:
            page = collected["pages"][row["page"]]
            body = page.get("body")
            rows = body.get("sDoTPeople", body).get("row", []) if isinstance(body, dict) else []
            if (
                row["row"] >= len(rows)
                or {k: rows[row["row"]].get(k) for k in OBSERVATION_FIELDS} != row["input"]
                or timestamp(row.get("received_at")).value
                != timestamp(page.get("received_at")).value
            ):
                raise ValueError("sensor_page_evidence_mismatch")
            row["payload_id"] = page_ids.get(row["page"])
        # Refresh the prerequisite after HTTP; store the exact boundary version used below.
        boundary = await ReferenceRepository(session).seoul_boundary()
        if boundary is None:
            raise ValueError("collect_sgis_boundary_first")
        data["boundary"] = {k: v for k, v in boundary.items() if k != "geometry"}
        spatial = SpatialRepository(session)
        selected, excluded = [], []
        async with session.begin_nested():
            for item in data["items"]:
                if await spatial.covers(boundary["geometry"], item["lng"], item["lat"]):
                    selected.append(item)
                else:
                    excluded.append(item["id"])
        data.update(
            items=selected,
            excluded_outside_seoul=len(excluded),
            excluded_sensor_codes=excluded,
            seoul_boundary_year=boundary["year"],
            observation_summary=observation_summary(selected),
        )
        derived = await repo.archive(
            run.id,
            data,
            None,
            kind="derived_snapshot",
            part="selected",
            legacy={"parent_payload_id": original.payload_id},
        )
        async with session.begin_nested():
            result = await session.run_sync(
                lambda sync: SensorWriter(sync).store(data, derived.payload_id, run.started_at)
            )
    except ValueError as exc:
        # Only local validation codes; do not expose arbitrary provider/SQL exception messages.
        error = (
            str(exc)
            if str(exc)
            in {
                "invalid_sensor_file_header",
                "missing_sdot_collection",
                "collect_sgis_boundary_first",
                "sensor_page_evidence_mismatch",
            }
            else "sensor_normalization_failed"
        )
    except Exception:
        error = "sensor_normalization_failed"
    result = result or {
        "reference_updated": False,
        "sensors_written": 0,
        "observations_written": 0,
        "sensor_results": [],
    }
    error = error or result.get("error_code")
    count = len(data["items"]) if data else 0
    accepted = result["sensors_written"]
    counts = ProcessingCounts(
        unit="sensor",
        scope="sdot_reference_and_attempt_storage",
        received=count,
        accepted=accepted,
        rejected=count - accepted,
        rejection_reasons={error: count - accepted} if error and count > accepted else {},
    )
    collection_failed = collected is None or (
        not collected["rows"] and any(p.get("error_code") for p in collected["pages"])
    )
    partial = bool(
        data
        and (
            data["collection_issues"]
            or data["location_quality"]["rejected"]
            or data["observation_quality"]["rejected"]
            or not result.get("reference_complete")
            or any(r["status"] not in {"valid", "unobserved"} for r in result["sensor_results"])
        )
    )
    status = (
        "failed"
        if error in {"sensor_normalization_failed", "sensor_page_evidence_mismatch"}
        or collected is None
        or collection_failed
        else "partial"
        if error or partial
        else "success"
    )
    error = error or (
        "sdot_collection_failed"
        if collection_failed
        else "partial_sdot_storage"
        if partial
        else None
    )
    report = {
        "source": "sdot_people",
        "run_id": str(run.id),
        "status": status,
        "error_code": error,
        "records": count,
        **counts.as_dict(),
        "storage": result,
        "observation_summary": data.get("observation_summary") if data else None,
        "observation_quality": data.get("observation_quality") if data else None,
        "location_quality": data.get("location_quality") if data else None,
        "query": collected["query"] if collected else None,
        "query_complete": collected["query_complete"] if collected else False,
        "normalized_storage": "applied" if accepted else "held",
        "reference_payload_id": str(original.payload_id),
        "page_payload_ids": page_ids,
        "derived_payload_id": str(derived.payload_id) if derived else None,
    }
    await repo.archive(
        run.id,
        report,
        None,
        kind="processing_report",
        part="storage",
        legacy={"parent_payload_id": derived.payload_id if derived else original.payload_id},
    )
    await repo.finish(
        run, status=status, error_code=error, completeness="unknown", **counts.database_counts()
    )
    return report
