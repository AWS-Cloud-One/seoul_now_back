"""Single-area collection and full citydata cycles with parking projection."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import CollectionRun
from app.modules.data_pipeline.pipelines.parking import project_citydata_parking
from app.modules.data_pipeline.pipelines.snapshots import normalize_snapshot
from app.repositories.collection import CollectionRepository

SOURCE = "seoul_citydata"


async def run_citydata_batch(engine, fetch_area, targets, *, report=None):
    """Finish a complete collection/projection cycle and report both stages' outcomes."""
    if not targets:
        raise ValueError("empty_citydata_targets")
    counts = Counter()
    for code, name in targets.items():
        result = await run_citydata(engine, lambda name=name: fetch_area(name), name)
        counts[result["status"]] += 1
        if report:
            report(
                {
                    "area_code": code,
                    "area_name": name,
                    "status": result["status"],
                    "error_code": result.get("error_code"),
                }
            )
        await asyncio.sleep(0.25)
    try:
        projection = await project_citydata_parking(engine)
    except Exception:
        projection = {"status": "failed", "error_code": "parking_projection_failed"}
    statuses = set(counts) | {projection["status"]}
    status = (
        "failed"
        if "failed" in statuses
        else "success"
        if statuses == {"success"}
        else "skipped"
        if statuses == {"skipped"}
        else "partial"
    )
    return {
        "cycle_complete": True,
        "status": status,
        "total": len(targets),
        **{key: counts[key] for key in ("success", "partial", "failed", "skipped")},
        "parking_projection": projection,
    }


async def run_citydata(engine, fetch, requested_area):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = datetime.now(UTC)
    area_code = None
    lock_name = f"{SOURCE}:{requested_area.casefold()}"
    async with engine.connect() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": lock_name}
        )
        await lock.commit()
        if not acquired:
            return {
                "status": "skipped",
                "error_code": "already_running",
                "area_name": requested_area,
            }
        try:
            async with factory.begin() as session:
                await CollectionRepository(session).start(
                    SOURCE, "area_name:" + requested_area, run_id=run_id, started_at=started
                )
            status, error = "failed", None
            try:
                data = await fetch()
                received = datetime.now(UTC)
                async with factory.begin() as session:
                    payload = await CollectionRepository(session).archive(run_id, data, received)
                    payload_id = payload.payload_id
                if not isinstance(data, dict):
                    raise CollectionError("invalid_snapshot")
                area_code = str(data.get("AREA_CD") or "").strip()
                area_name = str(data.get("AREA_NM") or "").strip()
                if not area_code or not area_name:
                    raise CollectionError("missing_area_identity")
                async with factory.begin() as session:
                    issue = await session.run_sync(
                        lambda sync: normalize_snapshot(
                            sync, SOURCE, data, payload_id, received_at=received
                        )
                    )
                    run = await session.get(CollectionRun, run_id)
                    run.target_key = "area:" + area_code
                    await CollectionRepository(session).finish(
                        run,
                        status="partial" if issue else "success",
                        completeness="complete",
                        received=1,
                        accepted=0 if issue else 1,
                        rejected=1 if issue else 0,
                        rejection_reasons={issue: 1} if issue else {},
                    )
                    status = run.status
            except Exception as exc:
                status = "failed"
                error = str(exc) if isinstance(exc, CollectionError) else "internal_error"
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    await CollectionRepository(session).finish(
                        run, status="failed", error_code=error
                    )
            return dict(
                run_id=str(run_id),
                source=SOURCE,
                area_name=requested_area,
                area_code=area_code,
                status=status,
                error_code=error,
                received=int(status in {"success", "partial"}),
                accepted=int(status == "success"),
                rejected=int(status == "partial"),
                duplicates=0,
            )
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_name}
            )
            await lock.commit()
