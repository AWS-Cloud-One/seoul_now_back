"""Provider snapshot processing: archive input, normalize, and decide the stored outcome."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.models import Base
from app.modules.data_pipeline.identity import PROCESSOR_VERSION, fingerprint, source_target
from app.modules.data_pipeline.normalizers.citydata import CityDataError, validate_area
from app.modules.data_pipeline.normalizers.road_reference import reference_error
from app.modules.data_pipeline.normalizers.storage_fields import (
    administrative_area_fields,
    population,
    population_area_fields,
    region_boundary_fields,
    road_traffic_fields,
)
from app.modules.data_pipeline.results import NormalizationOutcome, ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.map import lock_map_layer
from app.repositories.normalization import NormalizedWriter
from app.repositories.road_reference import replace_administrative_areas, replace_road_reference


@dataclass(frozen=True)
class SnapshotResult:
    run_id: UUID
    status: str
    payload_id: UUID | None = None
    error_code: str | None = None
    processing: dict | None = None


def snapshot_outcome(data):
    datasets = data.get("datasets")
    return (
        "partial"
        if isinstance(datasets, dict)
        and any(isinstance(v, dict) and v.get("complete") is False for v in datasets.values())
        else "success"
    )


def normalize_snapshot(session, key, data, payload_id, *, received_at=None):
    """Select field conversion and persistence while keeping SQL in the writer."""
    if key in {"national_parking", "sdot_people"}:
        return key + "_requires_page_evidence"
    writer = NormalizedWriter(session, Base.metadata)
    source, target, _ = source_target(key)
    if key == "seoul_citydata":
        try:
            area, _ = validate_area(data)
        except CityDataError as exc:
            return str(exc)
        target = "area:" + area
    writer.validate_payload(source, target, payload_id, area_name=data.get("AREA_NM"))
    now = datetime.now(UTC)
    common = dict(
        last_payload_id=payload_id,
        processed_at=now,
        processor_version=PROCESSOR_VERSION,
        is_active=True,
    )
    if key.startswith("topis_geometry:"):
        return normalize_road_reference(writer, data, payload_id)
    elif key == "citydata_place_boundaries":
        lock_map_layer(session, "population")
        writer.source(key)
        for feature in data.get("features", []):
            writer.upsert_geometry(
                "population_areas",
                dict(**population_area_fields(feature), source=key, **common),
                ["area_code"],
            )
    elif key in {"sgis_areas", "sgis_seoul_boundary"}:
        writer.source(key)
        version = fingerprint(data.get("features", []))
        area_values = []
        missing_parents = 0
        for feature in data.get("features", []):
            if key == "sgis_areas":
                values = administrative_area_fields(feature, version)
                if values is None:
                    missing_parents += 1
                    continue
                area_values.append(dict(**values, source=key, **common))
            else:
                writer.upsert_geometry(
                    "region_boundaries",
                    dict(
                        **region_boundary_fields(feature, version, data.get("boundary_year")),
                        source=key,
                        **common,
                    ),
                    ["code_system", "region_code"],
                )
        quality = data.get("geometry_quality")
        if key == "sgis_areas":
            if len({v["dong_code"] for v in area_values}) != len(area_values):
                return "duplicate_sgis_area_code"
            replace_administrative_areas(
                writer,
                area_values,
                complete=(
                    data.get("complete") is True
                    and bool(area_values)
                    and not missing_parents
                    and (quality is None or quality["rejected"] == 0)
                ),
            )
            if missing_parents:
                return NormalizationOutcome(
                    "partial" if area_values else "failed",
                    "missing_sgis_parent_code",
                    ProcessingCounts(
                        unit="boundary",
                        scope="sgis_areas_geometry",
                        received=len(area_values) + missing_parents,
                        accepted=len(area_values),
                        rejected=missing_parents,
                        rejection_reasons={"missing_sgis_parent_code": missing_parents},
                    ),
                    {"updated": bool(area_values)},
                )
        if quality is not None:
            accepted = len(data.get("features", []))
            reasons = Counter(r["reason"] for r in quality["rows"] if r["status"] == "rejected")
            rejected = quality["received"] - accepted
            return NormalizationOutcome(
                "failed" if not accepted else "partial" if rejected else "success",
                "no_usable_boundary"
                if not accepted
                else "partial_boundary_geometry"
                if rejected
                else None,
                ProcessingCounts(
                    unit="boundary",
                    scope=key + "_geometry",
                    received=quality["received"],
                    accepted=accepted,
                    rejected=rejected,
                    rejection_reasons=dict(reasons),
                ),
                {"geometry_quality": quality, "updated": bool(accepted)},
            )
    elif key.startswith("topis:"):
        return normalize_traffic(writer, key.split(":", 1)[1], data, payload_id, received_at)
    elif key == "seoul_citydata":
        return normalize_population(writer, area, data, payload_id, received_at)
    return None


def normalize_population(writer, area, data, payload_id, received_at):
    details, updated = {"section_status": "invalid", "field_issues": {}}, False
    try:
        values = population(data, received_at=received_at)
        details = values.pop("quality")
        error = details.pop("error_code")
    except CityDataError as exc:
        error = str(exc)
        details["section_status"] = {
            "missing_population_section": "missing",
            "empty_population_section": "empty",
            "conflicting_population_rows": "conflicting",
        }.get(error, "invalid")
    if not error:
        if not writer.exists("population_areas", area_code=area):
            error = "missing_population_geometry"
        else:
            writer.source("seoul_citydata")
            updated = writer.upsert(
                "population_latest",
                dict(
                    source="seoul_citydata",
                    area_code=area,
                    payload_id=payload_id,
                    processed_at=datetime.now(UTC),
                    processor_version=PROCESSOR_VERSION,
                    **values,
                ),
                ["source", "area_code"],
                newer_payload=True,
                return_changed=True,
            )
            error = (
                "population_time_guard"
                if not updated
                else "partial_population_values"
                if details["field_issues"]
                else None
            )
    return NormalizationOutcome(
        "partial" if error else "success",
        error,
        ProcessingCounts(
            unit="snapshot",
            scope="population_normalization",
            received=1,
            accepted=int(updated),
            rejected=int(not updated),
            conflicts=int(error == "conflicting_population_rows"),
            rejection_reasons={error: 1} if error and not updated else {},
        ),
        {**details, "updated": updated},
    )


def normalize_traffic(writer, link, data, payload_id, received_at):
    """A partial observation is one response; never fill its gaps from older values."""
    details = {"source_time_status": "unknown", "value_status": "not_evaluated", "updated": False}

    def outcome(status, error=None, *, updated=False):
        return NormalizationOutcome(
            status,
            error,
            ProcessingCounts(
                unit="response",
                scope="road_traffic_normalization",
                received=1,
                accepted=int(updated),
                rejected=int(not updated),
                rejection_reasons={error: 1} if error and not updated else {},
            ),
            {**details, "updated": updated},
        )

    if not writer.exists("road_links", link_id=link):
        return outcome("partial", "missing_road_geometry")
    rows = data.get("rows")
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("link_id") != link
    ):
        return outcome("partial", "invalid_link_response")
    values = road_traffic_fields(rows[0])
    details.update(
        {key: values.pop(key) for key in ("value_status", "field_issues", "value_counts")}
    )
    if details["value_status"] in {"missing", "invalid"}:
        return outcome("failed", values["reason_code"].lower())
    if received_at is None:
        return outcome("partial", "missing_traffic_receipt")
    updated = writer.traffic(link, {**values, "received_at": received_at}, payload_id)
    if not updated:
        return outcome("partial", "traffic_time_guard")
    return (
        outcome("partial", "partial_traffic_values", updated=True)
        if details["value_status"] == "partial"
        else outcome("success", updated=True)
    )


def normalize_road_reference(writer, data, payload_id):
    report = data.get("report", {})
    total = report.get("file_links", len(data.get("features", [])))
    error = reference_error(data, require_spatial=True)
    updated = False
    if error is None:
        updated = replace_road_reference(writer, data, payload_id)
        if not updated:
            error = "road_reference_time_guard"
    accepted = len(data.get("features", [])) if updated else 0
    rejected = total - accepted
    warnings = any(
        report.get(k)
        for k in (
            "excluded_sequence_gap_links",
            "unmatched_mapping_rows",
            "geometry_without_mapping",
            "ambiguous_mapping_positions",
            "unknown_sequence_rows",
            "quarantined_service_ids",
        )
    )
    return NormalizationOutcome(
        "failed" if error else "partial" if warnings else "success",
        error,
        ProcessingCounts(
            unit="link",
            scope="road_reference_import",
            received=total,
            accepted=accepted,
            rejected=rejected,
            rejection_reasons=(
                {error: rejected}
                if error
                else {
                    **(
                        {"excluded_vertex_sequence": len(report["excluded_sequence_gap_links"])}
                        if report.get("excluded_sequence_gap_links")
                        else {}
                    ),
                    **(
                        {"quarantined_road_geometry": len(report["quarantined_service_ids"])}
                        if report.get("quarantined_service_ids")
                        else {}
                    ),
                }
            )
            if rejected
            else {},
        ),
        {
            "updated": updated,
            "quality": report,
            "reference_id": data.get("reference", {}).get("reference_id"),
        },
    )


async def save_snapshot(
    session, source, data, *, received_at=None, payload_kind=None, source_files=None
) -> SnapshotResult | None:
    """Return the stored outcome; callers must commit before acting on a failure.

    Normalization failures retain their input and run record in the outer transaction.
    A redundant success/partial attempt has no separate record and returns None.
    """
    code, target, kind = source_target(source)
    if source == "seoul_citydata":
        try:
            area, _ = validate_area(data)
            target = "area:" + area
        except CityDataError:
            pass  # Archive malformed input; normalization records the identity failure.
    repo = CollectionRepository(session)
    if source == "seoul_citydata":
        from app.modules.data_pipeline.pipelines.citydata_storage import store_citydata

        run = await repo.start(code, target)
        result = await store_citydata(session, run, data, received_at, normalize_snapshot)
        return SnapshotResult(
            run.id, result["status"], UUID(result["input_payload_id"]), result["error_code"], result
        )
    now = datetime.now(UTC)
    if kind == "attempt":
        if data.get("status") in {"success", "partial"}:
            return None
        run = await repo.start(code, target)
        await repo.finish(
            run, status="failed", error_code=str(data.get("error_code") or "provider_error")[:200]
        )
        return SnapshotResult(run.id, run.status, error_code=run.error_code)
    payload_kind = payload_kind or (
        "processing_report"
        if kind == "report"
        else "derived_snapshot"
        if source
        in {"citydata_place_boundaries", "sgis_areas", "sgis_seoul_boundary", "sdot_people"}
        or source.startswith("topis_geometry:")
        else "provider_response"
    )
    run = await repo.start(
        code,
        target,
        kind="import"
        if payload_kind == "derived_snapshot"
        else "reprocess"
        if kind == "report"
        else "collection",
    )
    received = received_at or now if payload_kind == "provider_response" else received_at
    if source_files:
        inputs = {k: dict(v) for k, v in data["reference"]["inputs"].items()}
        for name, body in source_files.items():
            original = await repo.archive_file(run.id, body, part=name, metadata=inputs[name])
            inputs[name]["payload_id"] = str(original.payload_id)
        data = {**data, "reference": {**data["reference"], "inputs": inputs}}
    payload = await repo.archive(run.id, data, received, kind=payload_kind)
    # A savepoint rolls back failed normalization while retaining the immutable input.
    try:
        async with session.begin_nested():
            outcome = await session.run_sync(
                lambda sync: normalize_snapshot(
                    sync, source, data, payload.payload_id, received_at=received
                )
            )
    except Exception:
        outcome = (
            normalize_road_reference(
                None, {**data, "validation_error": "normalization_failed"}, payload.payload_id
            )
            if source.startswith("topis_geometry:")
            else "normalization_failed"
        )

    if isinstance(outcome, NormalizationOutcome):
        status, error = outcome.status, outcome.error_code
        counters = outcome.counts.database_counts()
        processing = {**outcome.counts.as_dict(), **outcome.details}
    else:
        status = (
            "failed"
            if outcome == "normalization_failed"
            else "partial"
            if outcome
            else snapshot_outcome(data)
        )
        error = outcome
        counters = dict(
            received=1,
            accepted=0 if outcome else 1,
            rejected=1 if outcome else 0,
            rejection_reasons={outcome: 1} if outcome else {},
        )
        processing = None
    if source.startswith(("topis:", "topis_geometry:")) or source in {
        "seoul_citydata",
        "sdot_people",
        "sgis_areas",
        "sgis_seoul_boundary",
    }:
        processing = processing or {
            "unit": "response",
            "scope": "population_normalization"
            if source == "seoul_citydata"
            else "sdot_preprocessing"
            if source == "sdot_people"
            else "road_traffic_normalization",
            **counters,
            "source_time_status": "unknown",
            "value_status": "not_evaluated",
            "updated": False,
        }
        await repo.archive(
            run.id,
            {
                "status": status,
                "error_code": error,
                "input_payload_id": str(payload.payload_id),
                "received_at": received.isoformat() if received else None,
                **processing,
            },
            None,
            kind="processing_report",
            part="normalization",
            legacy={"parent_payload_id": payload.payload_id},
        )
    await repo.finish(
        run,
        status=status,
        completeness=("complete" if data.get("reference", {}).get("complete") else "partial")
        if source.startswith("topis_geometry:")
        else "partial"
        if snapshot_outcome(data) == "partial"
        else "unknown",
        error_code=error,
        **counters,
    )
    return SnapshotResult(run.id, run.status, payload.payload_id, run.error_code, processing)
