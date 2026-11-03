"""Validate and import official files using caller-owned execution resources."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.data_pipeline.collectors import official_files as collectors
from app.modules.data_pipeline.normalizers import official_files as normalizers
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.citydata import CityDataRepository
from app.repositories.spatial import SpatialRepository


async def import_topis_geometry(engine, vertices_path, mapping_path):
    data = await asyncio.to_thread(collectors.read_topis_files, vertices_path, mapping_path)
    points, excluded, file_links = normalizers.complete_vertices(
        normalizers.topis_vertices(data["vertices"])
    )
    mapping_rows = normalizers.topis_mapping(data["mapping"])
    mapping, inverse = {}, {}
    for row in mapping_rows:
        service, standard = row["service_link_id"], row["standard_link_id"]
        mapping.setdefault(service, []).append(standard)
        inverse.setdefault(standard, set()).add(service)
    async with async_sessionmaker(engine).begin() as session:
        spatial = SpatialRepository(session)
        features = normalizers.road_features(await spatial.road_lines(points, 5181), mapping)
        snapshots = await CityDataRepository(session).list_latest()
        city_links = normalizers.citydata_links(snapshot.data for snapshot in snapshots)
        official = {f["id"]: f for f in features}
        pairs = [
            (standard, service)
            for standard in sorted(city_links)
            for service in sorted(inverse.get(standard, set()))
            if service in official
        ]
        distances = []
        for standard, service in pairs[:: max(1, len(pairs) // 100)][:100]:
            geometry = normalizers.citydata_line(city_links[standard])
            if geometry is None:
                continue
            distances.append(
                await spatial.line_distance(official[service]["geometry"], geometry, 5181)
            )
        median = sorted(distances)[len(distances) // 2] if distances else None
        # Keep the existing upper-median gate; no road data is written before it passes.
        if len(distances) < 10 or median > 30:
            return {
                "status": "failed",
                "error_code": "coordinate_cross_validation_failed",
                "samples": len(distances),
                "median": median,
            }
        matched = {standard for standard, _ in pairs}
        report = {
            "file_links": file_links,
            "excluded_sequence_gap_links": excluded,
            "geometry_links": len(features),
            "vertices": len(points),
            "citydata_standard_links": len(city_links),
            "mapped_citydata_links": len(matched),
            "mapped_service_links": len({service for _, service in pairs}),
            "citydata_unmapped": sorted(set(city_links) - matched),
            "one_to_many_standard_links": sum(len(inverse[k]) > 1 for k in matched),
            "mapping_source": "OA-15061 2025-03-06",
            "crs_basis": (
                "GRS80TM interpreted as EPSG:5181; "
                "checked against officially mapped citydata WGS84 lines"
            ),
            "crs_check_samples": len(distances),
            "median_line_distance_m": round(median, 2),
            "source_sha256": data["source_sha256"],
        }
        result = await save_snapshot(
            session,
            "topis_geometry:2025",
            {
                "type": "FeatureCollection",
                "features": features,
                "report": report,
                "standard_to_services": {k: sorted(v) for k, v in inverse.items()},
                "mapping_rows": mapping_rows,
            },
        )
    return {
        **{k: v for k, v in report.items() if k != "citydata_unmapped"},
        "status": result.status,
        "error_code": result.error_code,
    }


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
    return {"status": result.status, "error_code": result.error_code, "records": len(features)}
