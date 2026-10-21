from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.clients.seoul import CollectionError
from app.models.parking import CollectionRun
from app.modules.data_pipeline.normalizers.parking import normalize_parking
from app.repositories.parking import ParkingRepository


async def run_parking(
    engine: AsyncEngine, fetch: Callable[[], Awaitable[list[dict]]], source: str
) -> dict:
    """One bounded run. Source lock prevents overlap; all valid writes commit atomically."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = datetime.now(UTC)
    counters = {"received": 0, "accepted": 0, "rejected": 0, "duplicates": 0}
    reasons: Counter = Counter()
    async with engine.connect() as lock_connection:
        locked = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:source))"), {"source": source}
        )
        await lock_connection.commit()
        if not locked:
            return {"status": "skipped", "error_code": "already_running"}
        try:
            async with factory.begin() as session:
                session.add(
                    CollectionRun(
                        id=run_id,
                        source=source,
                        started_at=started,
                        status="running",
                        **counters,
                        rejection_reasons={},
                    )
                )
            status, error_code = "failed", None
            try:
                rows = await fetch()
                if not isinstance(rows, list) or not rows:
                    raise CollectionError("empty_or_invalid_snapshot")
                counters["received"] = len(rows)
                normalized = {}
                for row in rows:
                    if not isinstance(row, dict):
                        counters["rejected"] += 1
                        reasons["invalid_row"] += 1
                        continue
                    try:
                        record = normalize_parking(row, started, source)
                    except ValueError as exc:
                        counters["rejected"] += 1
                        reasons[str(exc)] += 1
                        continue
                    previous = normalized.get(record["id"])
                    if previous:
                        if previous != record:
                            raise CollectionError("conflicting_duplicate")
                        counters["duplicates"] += 1
                    else:
                        normalized[record["id"]] = record
                counters["accepted"] = len(normalized)
                if not normalized:
                    raise CollectionError("no_valid_records")
                status = "partial" if counters["rejected"] else "success"
                async with factory.begin() as session:
                    await ParkingRepository(session).upsert(list(normalized.values()))
                    run = await session.get(CollectionRun, run_id)
                    run.status = status
                    run.finished_at = datetime.now(UTC)
                    run.rejection_reasons = dict(reasons)
                    for key, value in counters.items():
                        setattr(run, key, value)
            except Exception as exc:
                # Never serialize arbitrary exceptions: HTTP URLs contain credentials.
                error_code = str(exc) if isinstance(exc, CollectionError) else "internal_error"
                status = "failed"
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    run.status, run.error_code = status, error_code
                    run.finished_at = datetime.now(UTC)
                    run.rejection_reasons = dict(reasons)
                    for key, value in counters.items():
                        setattr(run, key, value)
            return {
                "run_id": str(run_id),
                "source": source,
                "status": status,
                **counters,
                "error_code": error_code,
                "rejection_reasons": dict(reasons),
            }
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
            )
            await lock_connection.commit()
