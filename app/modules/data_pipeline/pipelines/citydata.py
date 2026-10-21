"""Collect and atomically replace the latest OA-21285 snapshot for one hotspot."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.clients.seoul import CollectionError
from app.models.parking import CollectionRun
from app.repositories.citydata import CityDataRepository

SOURCE = "seoul_citydata"


async def run_citydata(
    engine: AsyncEngine, fetch: Callable[[], Awaitable[dict]], requested_area: str
) -> dict:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = datetime.now(UTC)
    lock_name = f"{SOURCE}:{requested_area.casefold()}"
    async with engine.connect() as lock_connection:
        locked = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:source))"), {"source": lock_name}
        )
        await lock_connection.commit()
        if not locked:
            return {
                "status": "skipped",
                "error_code": "already_running",
                "area_name": requested_area,
            }
        try:
            async with factory.begin() as session:
                session.add(
                    CollectionRun(
                        id=run_id,
                        source=SOURCE,
                        area_name=requested_area,
                        started_at=started,
                        status="running",
                        received=0,
                        accepted=0,
                        rejected=0,
                        duplicates=0,
                        rejection_reasons={},
                    )
                )
            status, error_code = "failed", None
            area_code = None
            try:
                data = await fetch()
                if not isinstance(data, dict):
                    raise CollectionError("invalid_snapshot")
                area_name = str(data.get("AREA_NM") or "").strip()
                area_code = str(data.get("AREA_CD") or "").strip()
                if not area_name or not area_code:
                    raise CollectionError("missing_area_identity")
                # CITYDATA is stored intact: modules evolve and may be partially absent.
                async with factory.begin() as session:
                    await CityDataRepository(session).upsert(area_code, area_name, data, started)
                    run = await session.get(CollectionRun, run_id)
                    run.area_name, run.area_code = area_name, area_code
                    run.status = "success"
                    run.received = run.accepted = 1
                    run.finished_at = datetime.now(UTC)
                status = "success"
            except Exception as exc:
                error_code = str(exc) if isinstance(exc, CollectionError) else "internal_error"
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    run.status, run.error_code = "failed", error_code
                    run.area_code = area_code
                    run.finished_at = datetime.now(UTC)
                status = "failed"
            return {
                "run_id": str(run_id),
                "source": SOURCE,
                "area_name": requested_area,
                "area_code": area_code,
                "status": status,
                "received": int(status == "success"),
                "accepted": int(status == "success"),
                "rejected": 0,
                "duplicates": 0,
                "error_code": error_code,
            }
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": lock_name}
            )
            await lock_connection.commit()
