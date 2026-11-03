"""Provider snapshot processing: archive input, normalize, and decide the stored outcome."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.models import Base
from app.modules.data_pipeline.identity import PROCESSOR_VERSION, fingerprint, source_target
from app.modules.data_pipeline.normalizers.storage_fields import (
    administrative_area_fields,
    population,
    population_area_fields,
    region_boundary_fields,
    road_link_fields,
    road_traffic_fields,
)
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter


@dataclass(frozen=True)
class SnapshotResult:
    run_id: UUID
    status: str
    payload_id: UUID | None = None
    error_code: str | None = None


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
    writer = NormalizedWriter(session, Base.metadata)
    source, target, _ = source_target(key)
    if key == "seoul_citydata":
        target = "area:" + str(data.get("AREA_CD") or "")
    writer.validate_payload(source, target, payload_id, area_name=data.get("AREA_NM"))
    now = datetime.now(UTC)
    common = dict(
        last_payload_id=payload_id,
        processed_at=now,
        processor_version=PROCESSOR_VERSION,
        is_active=True,
    )
    if key.startswith("topis_geometry:"):
        writer.source("topis_geometry")
        writer.source("topis_standard_mapping")
        for feature in data.get("features", []):
            writer.upsert_geometry(
                "road_links",
                dict(**road_link_fields(feature, key.split(":", 1)[1]), source=source, **common),
                ["link_id"],
            )
        for mapping in data.get("mapping_rows", []):
            if writer.exists("road_links", link_id=mapping["service_link_id"]):
                writer.upsert(
                    "road_link_mappings",
                    dict(
                        **mapping,
                        source="topis_standard_mapping",
                        source_version="2025-03-06",
                        last_payload_id=payload_id,
                        processed_at=now,
                        processor_version=PROCESSOR_VERSION,
                    ),
                    ["service_link_id", "sequence", "standard_link_id"],
                )
    elif key == "citydata_place_boundaries":
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
        for feature in data.get("features", []):
            if key == "sgis_areas":
                values = administrative_area_fields(feature, version)
                if values is None:
                    return "missing_sgis_parent_code"
                writer.upsert_geometry(
                    "administrative_areas", dict(**values, source=key, **common), ["dong_code"]
                )
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
    elif key.startswith("topis:"):
        link = key.split(":", 1)[1]
        if not writer.exists("road_links", link_id=link):
            return "missing_road_geometry"
        rows = [row for row in data.get("rows", []) if str(row.get("link_id")) == link]
        if len(rows) != 1:
            return "invalid_link_response"
        writer.source("topis_traffic")
        writer.upsert(
            "road_traffic_latest",
            dict(
                source="topis_traffic",
                link_id=link,
                payload_id=payload_id,
                processed_at=now,
                processor_version=PROCESSOR_VERSION,
                **road_traffic_fields(rows[0]),
            ),
            ["source", "link_id"],
            newer_payload=True,
        )
    elif key == "seoul_citydata":
        area = str(data.get("AREA_CD") or "")
        if not writer.exists("population_areas", area_code=area):
            return "missing_population_geometry"
        values = population(data)
        if values:
            writer.source(key)
            writer.upsert(
                "population_latest",
                dict(
                    source=key,
                    area_code=area,
                    payload_id=payload_id,
                    processed_at=now,
                    processor_version=PROCESSOR_VERSION,
                    **values,
                ),
                ["source", "area_code"],
                newer_payload=True,
            )
    return None


async def save_snapshot(
    session, source, data, *, received_at=None, payload_kind=None
) -> SnapshotResult | None:
    """Return the stored outcome; callers must commit before acting on a failure.

    Normalization failures retain their input and run record in the outer transaction.
    A redundant success/partial attempt has no separate record and returns None.
    """
    code, target, kind = source_target(source)
    repo = CollectionRepository(session)
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
        if source in {"citydata_place_boundaries", "sgis_areas", "sgis_seoul_boundary"}
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
        outcome = "normalization_failed"

    status = (
        "failed"
        if outcome == "normalization_failed"
        else "partial"
        if outcome
        else snapshot_outcome(data)
    )
    await repo.finish(
        run,
        status=status,
        completeness="partial" if snapshot_outcome(data) == "partial" else "unknown",
        error_code=outcome,
        received=1,
        accepted=0 if outcome else 1,
        rejected=1 if outcome else 0,
        rejection_reasons={outcome: 1} if outcome else {},
    )
    return SnapshotResult(run.id, run.status, payload.payload_id, run.error_code)
