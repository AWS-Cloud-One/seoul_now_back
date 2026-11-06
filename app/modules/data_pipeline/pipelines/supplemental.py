"""Supplemental collection, spatial processing and persistence with short DB transactions."""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors import supplemental as collectors
from app.modules.data_pipeline.pipelines.boundaries import save_areas
from app.modules.data_pipeline.pipelines.national_parking import save_national_parking
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.modules.data_pipeline.pipelines.sdot import save_sdot
from app.repositories.collection import CollectionRepository
from app.repositories.national_parking_progress import load_national_parking_progress
from app.repositories.reference import ReferenceRepository

SOURCES = {
    "areas": "sgis_areas",
    "sdot": "sdot_people",
    "parking": "national_parking",
}


async def run_supplemental(engine, http, settings, target):
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
            if target == "sdot":
                async with factory() as session:
                    boundary = await ReferenceRepository(session).seoul_boundary()
                    if boundary is None:
                        raise CollectionError("collect_sgis_boundary_first")

            # All network reads happen before the processing/write transaction.
            if target == "areas":
                started_at = datetime.now(UTC)
                collected = await collectors.collect_areas(http, settings)
                async with factory.begin() as session:
                    return await save_areas(session, collected, started_at=started_at)
            elif target == "sdot":
                started_at = datetime.now(UTC)
                async with factory() as session:
                    reference = await ReferenceRepository(session).sensor_reference()
                if reference is None:
                    raise CollectionError("import_sensor_reference_first")
                if reference["metadata"]["boundary_version"] != boundary["version"]:
                    raise CollectionError("refresh_sensor_reference_for_boundary")
                try:
                    collected = await collectors.collect_sdot(http, settings)
                    error = None
                except Exception as exc:
                    collected = None
                    error = (
                        str(exc) if isinstance(exc, CollectionError) else "sdot_collection_failed"
                    )
                async with factory.begin() as session:
                    return await save_sdot(
                        session, reference, collected, started_at=started_at, error_code=error
                    )
            else:
                async with factory() as session:
                    progress = await load_national_parking_progress(session)
                data = await collectors.collect_national_parking(http, settings, progress=progress)
                async with factory.begin() as session:
                    result = await save_national_parking(session, data)
                return await publish_result(engine, result, geometry=("parking",))

        except Exception as exc:
            code = str(exc) if isinstance(exc, CollectionError) else "provider_or_database_error"
            async with factory.begin() as session:
                repo = CollectionRepository(session)
                run = await repo.start(source, "all")
                await repo.finish(run, status="failed", error_code=code)
            return {"source": source, "status": "failed", "error_code": code}
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
            )
            await lock.commit()
