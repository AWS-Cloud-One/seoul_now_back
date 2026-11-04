"""Supplemental collection, spatial processing and persistence with short DB transactions."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.normalizers import supplemental as normalizers
from app.modules.data_pipeline.normalizers.sdot import sensor_locations
from app.modules.data_pipeline.pipelines.national_parking import save_national_parking
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.sdot import save_sdot
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.national_parking_progress import load_national_parking_progress
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository

SOURCES = {
    "areas": "sgis_areas",
    "sdot": "sdot_people",
    "cctv": "its_cctv",
    "parking": "national_parking",
}


async def prepare_areas(spatial, collected):
    areas, area_results, boundary_results = [], [], []

    async def polygon(value, *, geojson=False):
        try:
            async with spatial.session.begin_nested():
                return await spatial.checked_polygon(value, 5179, geojson=geojson)
        except ValueError, TypeError, SQLAlchemyError:
            return None, {"status": "rejected", "reason": "invalid_polygon_input"}

    for district, row in collected["areas"]:
        geometry, quality = await polygon(row.get("pg"))
        area_results.append({"code": row.get("cd"), **quality})
        if geometry is not None:
            areas.append(normalizers.area_feature(district, row, geometry))
    boundaries = []
    for feature in collected["boundary"]["features"]:
        geometry, quality = await polygon(feature.get("geometry"), geojson=True)
        boundary_results.append({"code": "11", **quality})
        if geometry is not None:
            boundaries.append({**feature, "geometry": geometry})
    data, boundary = normalizers.area_snapshot(areas), normalizers.boundary_snapshot(boundaries)
    for snapshot, results, original in (
        (data, area_results, collected["areas"]),
        (boundary, boundary_results, collected["boundary"]),
    ):
        snapshot["source_input"] = original
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


async def filter_inside(spatial, boundary, items):
    return [item for item in items if await spatial.covers(boundary, item["lng"], item["lat"])]


async def run_supplemental(engine, http, settings, target, *, sensor_path=None, probe_path=None):
    source = SOURCES[target]
    factory = async_sessionmaker(engine)
    async with engine.connect() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:source))"), {"source": source}
        )
        # Session-level locks survive commit; HTTP must not keep an idle transaction open.
        await lock.commit()
        if not acquired:
            return {"source": source, "status": "skipped"}
        try:
            boundary = None
            if target in {"sdot", "cctv"}:
                async with factory() as session:
                    boundary = await ReferenceRepository(session).seoul_boundary()
                    if boundary is None:
                        raise CollectionError("collect_sgis_boundary_first")

            # All network and file reads happen before the processing/write transaction.
            if target == "areas":
                collected = await collectors.collect_areas(http, settings)
            elif target == "sdot":
                started_at = datetime.now(UTC)
                sensor_file = await asyncio.to_thread(collectors.read_sensor_file, sensor_path)
                try:
                    sensor_locations(sensor_file)
                except ValueError:
                    async with factory.begin() as session:
                        return await save_sdot(session, sensor_file, None, started_at=started_at)
                collected = await collectors.collect_sdot(http, settings)
                async with factory.begin() as session:
                    return await save_sdot(session, sensor_file, collected, started_at=started_at)
            elif target == "cctv":
                collected = await collectors.collect_cctv(http, settings, probe_path=probe_path)
                candidates = normalizers.cctv_candidates(collected["rows"])
            else:
                async with factory() as session:
                    progress = await load_national_parking_progress(session)
                data = await collectors.collect_national_parking(http, settings, progress=progress)
                async with factory.begin() as session:
                    result = await save_national_parking(session, data)
                return await publish_result(engine, result, geometry=("parking",))

            async with factory.begin() as session:
                spatial = SpatialRepository(session)
                results = []
                if target == "areas":
                    data, boundary_data = await prepare_areas(spatial, collected)
                    results.append(
                        await save_snapshot(session, "sgis_seoul_boundary", boundary_data)
                    )
                elif target == "cctv":
                    filtered = await filter_inside(spatial, boundary["geometry"], candidates)
                    data = normalizers.cctv_snapshot(filtered, collected)
                results.append(await save_snapshot(session, source, data))
            result = max(results, key=lambda r: {"success": 0, "partial": 1, "failed": 2}[r.status])
            return {
                "source": source,
                "status": result.status,
                "error_code": result.error_code,
                "records": (
                    sum(len(dataset["rows"]) for dataset in data["datasets"].values())
                    if "datasets" in data
                    else len(data.get("items", data.get("features", [])))
                ),
                **(
                    {
                        "geometry_quality": data["geometry_quality"],
                        "boundary_quality": boundary_data["geometry_quality"],
                    }
                    if target == "areas"
                    else {}
                ),
            }
        except Exception as exc:
            code = str(exc) if isinstance(exc, CollectionError) else "provider_or_database_error"
            async with factory.begin() as session:
                await save_snapshot(
                    session, source + ":attempt", {"status": "failed", "error_code": code}
                )
            return {"source": source, "status": "failed", "error_code": code}
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
            )
            await lock.commit()
