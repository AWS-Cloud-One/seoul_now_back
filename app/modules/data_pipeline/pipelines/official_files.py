"""Validate parsed reference values and atomically replace current DB reference sets."""

import re
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Base
from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers import official_files as normalizers
from app.modules.data_pipeline.normalizers.road_geometry import (
    MATCH_POLICY,
    citydata_geometry_index,
    classify_match,
    validation_outcome,
)
from app.modules.data_pipeline.normalizers.road_reference import (
    prepare_reference,
    reference_error,
    reference_metadata,
)
from app.modules.data_pipeline.normalizers.sdot import sensor_locations
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.repositories.collection import CollectionRepository
from app.repositories.normalization import NormalizedWriter
from app.repositories.reference import ReferenceRepository
from app.repositories.reference_import import ReferenceImportWriter
from app.repositories.road_reference import replace_road_reference
from app.repositories.spatial import SpatialRepository


async def apply_reference(engine, source, target, started_at, received, operation):
    """One import transaction; validation/storage failures roll back the whole reference set."""
    async with async_sessionmaker(engine).begin() as session:
        repo = CollectionRepository(session)
        run = await repo.start(source, target, started_at=started_at, kind="import")
        try:
            async with session.begin_nested():
                result = await operation(session)
                await session.flush()
        except (ValueError, KeyError, TypeError, SQLAlchemyError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else "reference_validation_failed"
            if not re.fullmatch(r"[a-z][a-z0-9_]*", error):
                error = "reference_validation_failed"
            result = dict(status="failed", error_code=error, records=0)
        accepted = result["records"]
        await repo.finish(
            run,
            status=result["status"],
            error_code=result.get("error_code"),
            received=received,
            accepted=accepted,
            rejected=received - accepted,
            completeness="complete" if result["status"] != "failed" else "unknown",
        )
    return {"source": source, **result}


async def import_topis_geometry(
    engine,
    data,
    *,
    vertices_version,
    mapping_version,
    road_inputs,
    vertices_date=None,
    mapping_date=None,
    complete=True,
    allowed_exclusions=(),
    expected_hashes=None,
    started_at=None,
):
    started_at = started_at or datetime.now(UTC)

    async def apply(session):
        reference = reference_metadata(
            data,
            vertices_version=vertices_version,
            mapping_version=mapping_version,
            vertices_date=vertices_date,
            mapping_date=mapping_date,
            complete=complete,
            allowed_exclusions=allowed_exclusions,
        )
        if any(data[name]["sha256"] != digest for name, digest in (expected_hashes or {}).items()):
            raise ValueError("reference_file_changed_requires_version")
        points, mapping_rows, report = prepare_reference(data)
        spatial = SpatialRepository(session)
        mapping = {}
        for row in mapping_rows:
            mapping.setdefault(row["service_link_id"], []).append(row["standard_link_id"])
        candidate = dict(
            reference=reference,
            report=report,
            mapping_rows=mapping_rows,
            features=normalizers.road_features(
                await spatial.road_lines(points, 5181),
                mapping,
                version=vertices_version,
                source_srid=5181,
            ),
        )
        error = reference_error(candidate)
        if error:
            raise ValueError(error)
        spatial_report = await check_road_geometry(
            spatial,
            candidate["features"],
            mapping_rows,
            road_inputs,
        )
        report["spatial_validation"] = spatial_report
        if spatial_report["error_code"]:
            raise ValueError(spatial_report["error_code"])
        quarantined = set(spatial_report.get("quarantined_service_ids", []))
        report["quarantined_service_ids"] = sorted(quarantined)
        candidate["features"] = [f for f in candidate["features"] if f["id"] not in quarantined]
        candidate["mapping_rows"] = [
            r for r in mapping_rows if r["service_link_id"] not in quarantined
        ]
        error = reference_error(candidate, require_spatial=True)
        if error:
            raise ValueError(error)
        await session.run_sync(
            lambda sync: replace_road_reference(
                NormalizedWriter(sync, Base.metadata),
                candidate,
                started_at,
            )
        )
        warnings = any(
            report.get(key)
            for key in (
                "excluded_sequence_gap_links",
                "unmatched_mapping_rows",
                "geometry_without_mapping",
                "ambiguous_mapping_positions",
                "unknown_sequence_rows",
                "quarantined_service_ids",
            )
        )
        return dict(
            status="partial" if warnings else "success",
            error_code=None,
            records=len(candidate["features"]),
            excluded_links=report["excluded_sequence_gap_links"],
            quarantined_links=sorted(quarantined),
        )

    # Count unique IDs only; full row validation remains inside the import savepoint.
    count = len({r.get("A") for r in data["vertices"]["rows"] if r})
    result = await apply_reference(
        engine, "topis_geometry", "file:topis_vertices", started_at, count, apply
    )
    return await publish_result(engine, result, geometry=("roads",))


async def check_road_geometry(spatial, features, mapping_rows, inputs):
    index, issues = citydata_geometry_index(inputs)
    official = {f["id"]: f["geometry"] for f in features}
    # Deduplicate repeated ordinals, but retain every explicitly mapped service/standard pair.
    correspondences = sorted(
        {
            (r["standard_link_id"], r["service_link_id"])
            for r in mapping_rows
            if r["service_link_id"] in official
        }
    )
    pairs = []
    for standard, service in correspondences:
        if standard not in index or index[standard]["state"] != "valid":
            continue
        metrics = await spatial.line_match_metrics(
            official[service],
            index[standard]["geometry"],
            tolerance_m=MATCH_POLICY["tolerance_m"],
            segment_length_m=MATCH_POLICY["segment_length_m"],
        )
        pairs.append(
            {"standard_link_id": standard, "service_link_id": service, **classify_match(metrics)}
        )
    return validation_outcome(
        pairs, index, len(official), {standard for standard, _ in correspondences}, issues
    )


async def import_place_boundaries(engine, targets, shapes, *, started_at=None):
    started_at = started_at or datetime.now(UTC)

    async def apply(session):
        normalizers.validate_place_reference(targets, shapes)
        spatial, features = SpatialRepository(session), []
        for values, rings in shapes:
            geometry, repaired = await spatial.polygon_from_rings(normalizers.rings_wkt(rings))
            features.append(normalizers.place_feature(values, geometry, repaired))
        version = fingerprint({"targets": targets, "features": features})
        await session.run_sync(
            lambda sync: ReferenceImportWriter(sync).places(
                features,
                version,
                started_at,
            )
        )
        return dict(
            status="success", error_code=None, records=len(features), source_version=version
        )

    result = await apply_reference(
        engine,
        "citydata_place_boundaries",
        "file:places",
        started_at,
        len(shapes),
        apply,
    )
    return await publish_result(engine, result, geometry=("population",))


async def import_sensor_locations(engine, file, *, reference_date=None, started_at=None):
    started_at = started_at or datetime.now(UTC)

    async def apply(session):
        reference = sensor_locations(file)
        if reference["quality"]["rejected"] or not reference["locations"]:
            raise ValueError("invalid_sensor_reference")
        boundary = await ReferenceRepository(session).seoul_boundary()
        if boundary is None:
            raise ValueError("collect_sgis_boundary_first")
        spatial = SpatialRepository(session)
        selected = [
            item
            for item in reference["locations"].values()
            if await spatial.covers(boundary["geometry"], item["lng"], item["lat"])
        ]
        await session.run_sync(
            lambda sync: ReferenceImportWriter(sync).sensors(
                selected,
                version=file["sha256"],
                reference_date=reference_date or file.get("reference_date"),
                boundary_version=boundary["version"],
                imported_at=started_at,
            )
        )
        return dict(
            status="success",
            error_code=None,
            records=len(selected),
            excluded_outside_seoul=len(reference["locations"]) - len(selected),
        )

    return await apply_reference(
        engine, "sdot_people", "file:installation", started_at, len(file["rows"]), apply
    )
