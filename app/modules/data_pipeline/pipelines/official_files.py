"""Validate and import official files using caller-owned execution resources."""

import asyncio

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.data_pipeline.collectors import official_files as collectors
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
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.repositories.spatial import SpatialRepository


async def import_topis_geometry(
    engine,
    vertices_path,
    mapping_path,
    *,
    vertices_version,
    mapping_version,
    vertices_date=None,
    mapping_date=None,
    complete=True,
    allowed_exclusions=(),
    expected_hashes=None,
):
    data = await asyncio.to_thread(collectors.read_topis_files, vertices_path, mapping_path)
    reference = reference_metadata(
        data,
        vertices_version=vertices_version,
        mapping_version=mapping_version,
        vertices_date=vertices_date,
        mapping_date=mapping_date,
        complete=complete,
        allowed_exclusions=allowed_exclusions,
    )
    candidate = {"type": "FeatureCollection", "features": [], "reference": reference}
    try:
        points, mapping_rows, report = prepare_reference(data)
        candidate.update(mapping_rows=mapping_rows, report=report)
        if any(data[name]["sha256"] != digest for name, digest in (expected_hashes or {}).items()):
            candidate["validation_error"] = "reference_file_changed_requires_version"
    except (ValueError, KeyError, TypeError) as exc:
        candidate["validation_error"] = (
            str(exc) if isinstance(exc, ValueError) else "invalid_reference_row"
        )
    async with async_sessionmaker(engine).begin() as session:
        if not candidate.get("validation_error"):
            spatial = SpatialRepository(session)
            mapping = {}
            for row in mapping_rows:
                service, standard = row["service_link_id"], row["standard_link_id"]
                mapping.setdefault(service, []).append(standard)
            try:
                async with session.begin_nested():
                    candidate["features"] = normalizers.road_features(
                        await spatial.road_lines(points, 5181),
                        mapping,
                        version=vertices_version,
                        source_srid=5181,
                    )
            except ValueError, SQLAlchemyError:
                candidate["validation_error"] = "invalid_road_geometry"
            error = reference_error(candidate)
            if error:
                candidate["validation_error"] = error
            else:
                snapshots = await CityDataRepository(session).list_latest(section="road_geometry")
                try:
                    async with session.begin_nested():
                        spatial_report = await check_road_geometry(
                            spatial, candidate["features"], mapping_rows, snapshots
                        )
                except ValueError, SQLAlchemyError:
                    spatial_report = {
                        "status": "held",
                        "error_code": "spatial_validation_failed",
                        "policy": dict(MATCH_POLICY),
                        "all_roads_verified": False,
                    }
                spatial_report["section_inputs"] = [s.evidence for s in snapshots]
                if any(s.evidence["latest_attempt"]["state"] == "invalid" for s in snapshots):
                    spatial_report.update(status="held", error_code="invalid_latest_road_section")
                report["spatial_validation"] = spatial_report
                if spatial_report["error_code"]:
                    candidate["validation_error"] = spatial_report["error_code"]
                else:
                    quarantined = set(spatial_report.get("quarantined_service_ids", []))
                    report["quarantined_service_ids"] = sorted(quarantined)
                    report["quarantined_features"] = [
                        f for f in candidate["features"] if f["id"] in quarantined
                    ]
                    report["quarantined_mapping_rows"] = [
                        r for r in candidate["mapping_rows"] if r["service_link_id"] in quarantined
                    ]
                    candidate["features"] = [
                        f for f in candidate["features"] if f["id"] not in quarantined
                    ]
                    candidate["mapping_rows"] = [
                        r
                        for r in candidate["mapping_rows"]
                        if r["service_link_id"] not in quarantined
                    ]
                    report["accepted_geometry_links"] = len(candidate["features"])
                    active_ids = {f["id"] for f in candidate["features"]}
                    report["accepted_mapping_rows"] = sum(
                        r["service_link_id"] in active_ids for r in candidate["mapping_rows"]
                    )
        result = await save_snapshot(
            session,
            "topis_geometry:" + vertices_version,
            candidate,
            source_files={name: file["body"] for name, file in data.items()},
        )
    report = {
        "status": result.status,
        "error_code": result.error_code,
        "reference": reference,
        "processing": result.processing,
        **candidate.get("report", {}),
    }
    return await publish_result(engine, report, geometry=("roads",))


async def check_road_geometry(spatial, features, mapping_rows, snapshots):
    inputs = [
        {
            "data": snapshot.data,
            "area_code": snapshot.area_code,
            "payload_id": str(snapshot.payload_id),
            "received_at": snapshot.received_at.isoformat() if snapshot.received_at else None,
        }
        for snapshot in snapshots
    ]
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


async def import_place_boundaries(engine, path):
    shapes = await asyncio.to_thread(collectors.read_shapes, path)
    async with async_sessionmaker(engine).begin() as session:
        spatial = SpatialRepository(session)
        features = []
        for values, rings in shapes:
            geometry, repaired = await spatial.polygon_from_rings(normalizers.rings_wkt(rings))
            features.append(normalizers.place_feature(values, geometry, repaired))
        result = await save_snapshot(
            session,
            "citydata_place_boundaries",
            {
                "type": "FeatureCollection",
                "features": features,
                "source_url": "https://data.seoul.go.kr/dataVisual/seoul/guide.do",
                "crs": "EPSG:4326",
                "notice": (
                    "서울시 공식 121장소 관측 경계. 행정동 경계와 다릅니다. "
                    "파일의 기준일은 별도 확인 필요."
                ),
            },
        )
    report = {"status": result.status, "error_code": result.error_code, "records": len(features)}
    return await publish_result(engine, report, geometry=("population",))
