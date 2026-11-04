"""Validate current SGIS geometry and commit accepted boundaries/relations together."""

from collections import Counter

from sqlalchemy.exc import SQLAlchemyError

from app.modules.data_pipeline.normalizers import supplemental as normalizers
from app.repositories.boundaries import store_boundaries
from app.repositories.collection import CollectionRepository
from app.repositories.spatial import SpatialRepository


async def prepare_areas(spatial, collected):
    areas, area_results, boundary_results = [], [], []

    async def polygon(value, *, geojson=False):
        try:
            async with spatial.session.begin_nested():
                return await spatial.checked_polygon(value, 5179, geojson=geojson)
        except ValueError, TypeError, SQLAlchemyError:
            return None, {"status": "rejected", "reason": "invalid_polygon_input"}

    codes = Counter(str(row.get("cd")) for _, row in collected["areas"] if isinstance(row, dict))
    for district, row in collected["areas"]:
        code = row.get("cd") if isinstance(row, dict) else None
        reason = (
            "invalid_sgis_area_identity"
            if not isinstance(row, dict)
            or not isinstance(district, dict)
            or not all(
                isinstance(value, str) and value.strip()
                for value in (
                    code,
                    row.get("addr_name"),
                    district.get("cd"),
                    district.get("addr_name"),
                )
            )
            else "duplicate_sgis_area_code"
            if codes[str(code)] > 1
            else None
        )
        if reason:
            area_results.append({"code": code, "status": "rejected", "reason": reason})
            continue
        geometry, quality = await polygon(row.get("pg"))
        area_results.append({"code": row.get("cd"), **quality})
        if geometry is not None:
            areas.append(normalizers.area_feature(district, row, geometry))
    boundaries = []
    for feature in collected["boundary"]["features"]:
        geometry, quality = await polygon(
            feature.get("geometry") if isinstance(feature, dict) else None, geojson=True
        )
        boundary_results.append({"code": "11", **quality})
        if geometry is not None:
            boundaries.append({"type": "Feature", "geometry": geometry})
    if len(boundaries) > 1:
        boundaries = []
        boundary_results = [
            {"code": "11", "status": "rejected", "reason": "ambiguous_seoul_boundary"}
            for _ in boundary_results
        ]
    data, boundary = normalizers.area_snapshot(areas), normalizers.boundary_snapshot(boundaries)
    for snapshot, results in ((data, area_results), (boundary, boundary_results)):
        snapshot["geometry_quality"] = {
            "received": len(results),
            "accepted": sum(r["status"] != "rejected" for r in results),
            "repaired": sum(r["status"] == "repaired" for r in results),
            "rejected": sum(r["status"] == "rejected" for r in results),
            "max_repair_area_change": 0.001,
            "rows": results,
        }
    data["complete"] = (
        collected.get("complete") is True
        and bool(areas)
        and data["geometry_quality"]["rejected"] == 0
        and len({feature["id"] for feature in areas}) == len(areas)
    )
    return data, boundary


async def save_areas(session, collected, *, started_at=None):
    repo = CollectionRepository(session)
    run = await repo.start("sgis_areas", "all", started_at=started_at)
    data, boundary = await prepare_areas(SpatialRepository(session), collected)
    qualities = [data["geometry_quality"], boundary["geometry_quality"]]
    received = sum(q["received"] for q in qualities)
    accepted = len(data["features"]) + len(boundary["features"])
    reasons = Counter(
        row["reason"] for q in qualities for row in q["rows"] if row["status"] == "rejected"
    )
    incomplete = not data["complete"] or not boundary["features"]
    error = None
    try:
        async with session.begin_nested():
            await session.run_sync(lambda sync: store_boundaries(sync, data, boundary))
    except Exception:
        reasons["boundary_storage_failed"] += accepted
        accepted = 0
        error = "boundary_storage_failed"
    status = "failed" if not accepted else "partial" if reasons or incomplete else "success"
    error = error or (
        "no_usable_boundary"
        if not accepted
        else "partial_boundary_geometry"
        if reasons or incomplete
        else None
    )
    await repo.finish(
        run,
        status=status,
        error_code=error,
        received=received,
        accepted=accepted,
        rejected=received - accepted,
        rejection_reasons=dict(reasons),
        completeness="partial" if incomplete or reasons else "complete",
    )
    return dict(
        source="sgis_areas",
        status=status,
        error_code=error,
        records=accepted,
        geometry_quality=data["geometry_quality"],
        boundary_quality=boundary["geometry_quality"],
        collection_issues=collected.get("issues", []),
    )
