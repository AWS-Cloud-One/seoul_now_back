"""Supplemental collection, spatial processing and persistence with short DB transactions."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.normalizers import supplemental as normalizers
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository

SOURCES = {
    "areas": "sgis_areas",
    "sdot": "sdot_people",
    "cctv": "its_cctv",
    "parking": "national_parking",
}


async def prepare_areas(spatial, collected):
    areas = []
    for district, row in collected["areas"]:
        geometry = await spatial.transform_wkt(row["pg"], 5179)
        areas.append(normalizers.area_feature(district, row, geometry))
    boundaries = []
    for feature in collected["boundary"]["features"]:
        geometry = await spatial.transform_geojson(feature["geometry"], 5179)
        boundaries.append({**feature, "geometry": geometry})
    return normalizers.area_snapshot(areas), normalizers.boundary_snapshot(boundaries)


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
                sensor_rows = await asyncio.to_thread(collectors.read_sensor_rows, sensor_path)
                locations = normalizers.sensor_locations(sensor_rows)
                collected = await collectors.collect_sdot(http, settings)
                data = normalizers.sdot_snapshot(locations, collected)
            elif target == "cctv":
                collected = await collectors.collect_cctv(http, settings, probe_path=probe_path)
                candidates = normalizers.cctv_candidates(collected["rows"])
            else:
                data = await collectors.collect_national_parking(http, settings)

            async with factory.begin() as session:
                spatial = SpatialRepository(session)
                results = []
                if target == "areas":
                    data, boundary_data = await prepare_areas(spatial, collected)
                    results.append(
                        await save_snapshot(session, "sgis_seoul_boundary", boundary_data)
                    )
                elif target == "sdot":
                    filtered = await filter_inside(spatial, boundary["geometry"], data["items"])
                    data["excluded_outside_seoul"] = len(data["items"]) - len(filtered)
                    data["items"] = filtered
                    data["seoul_boundary_year"] = boundary["year"]
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
