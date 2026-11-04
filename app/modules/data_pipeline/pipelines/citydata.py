"""Single-area collection and full citydata cycles with parking projection."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.models import CollectionRun
from app.modules.data_pipeline.collectors.citydata import CityDataDocument, CityDataResponseError
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.pipelines.citydata_storage import store_citydata
from app.modules.data_pipeline.pipelines.parking import project_citydata_parking
from app.modules.data_pipeline.pipelines.publication import publication_summary, publish_result
from app.modules.data_pipeline.pipelines.snapshots import normalize_snapshot
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository

SOURCE = "seoul_citydata"
STOP_ERRORS = {"provider_auth_error", "provider_quota_exceeded", "missing_citydata_api_key"}


async def run_citydata_batch(engine, fetch_area, targets, *, report=None):
    """Finish a complete collection/projection cycle and report both stages' outcomes."""
    if not targets:
        raise ValueError("empty_citydata_targets")
    counts = Counter()
    current_runs, stop_reason = {}, None
    for code, name in targets.items():
        result = await run_citydata(
            engine,
            lambda code=code, name=name: fetch_area(code, name),
            name,
            expected_code=code,
            _publish=False,
        )
        counts[result["status"]] += 1
        current_runs[code] = result.get("run_id")
        if report:
            report(
                {
                    "area_code": code,
                    "area_name": name,
                    "status": result["status"],
                    "error_code": result.get("error_code"),
                }
            )
        if result.get("error_code") in STOP_ERRORS:
            stop_reason = result["error_code"]
            break
        await asyncio.sleep(0.25)
    try:
        projection = await project_citydata_parking(engine, current_run_ids=current_runs)
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
    result = {
        "cycle_complete": stop_reason is None,
        "status": "paused_provider_limit" if stop_reason else status,
        "stop_reason": stop_reason,
        "completed": sum(counts.values()),
        "remaining": len(targets) - sum(counts.values()),
        "total": len(targets),
        **{key: counts[key] for key in ("success", "partial", "failed", "skipped")},
        "parking_projection": projection,
    }
    result = await publish_result(engine, result, observations=("population",))
    result["publication"] = publication_summary(
        {
            **result["publication"]["layers"],
            **projection.get("publication", {}).get("layers", {}),
        }
    )
    return result


async def run_citydata(engine, fetch, requested_area, *, expected_code, _publish=True):
    requested_area, expected_code = requested_area.strip(), expected_code.strip()
    if not requested_area or not expected_code:
        raise CollectionError("missing_area_identity")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = datetime.now(UTC)
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
                    SOURCE, "area:" + expected_code, run_id=run_id, started_at=started
                )
            try:
                try:
                    data = await fetch()
                except CityDataResponseError as exc:
                    data = exc.document  # Rejected envelopes are still immutable evidence.
                document = (
                    data
                    if isinstance(data, CityDataDocument)
                    else CityDataDocument(data, datetime.now(UTC), require_envelope=False)
                )
                # Commit the original before normalization; unexpected failures must not lose it.
                async with factory.begin() as session:
                    original = await CollectionRepository(session).archive(
                        run_id, document.body, document.received_at
                    )
                async with factory.begin() as session:
                    run = await session.get(CollectionRun, run_id)
                    result = await store_citydata(
                        session,
                        run,
                        document.body,
                        document.received_at,
                        normalize_snapshot,
                        expected_code=expected_code,
                        expected_name=requested_area,
                        original=original,
                        require_envelope=document.require_envelope,
                    )
            except Exception as exc:
                error = (
                    str(exc)
                    if isinstance(exc, (CollectionError, CityDataError))
                    else "internal_error"
                )
                counts = ProcessingCounts(unit="section", scope="citydata_section_processing")
                result = {
                    "status": "failed",
                    "error_code": error,
                    **counts.as_dict(),
                    "sections": {},
                }
                async with factory.begin() as session:
                    repo = CollectionRepository(session)
                    run = await session.get(CollectionRun, run_id)
                    await repo.archive(
                        run_id, result, None, kind="processing_report", part="sections"
                    )
                    await repo.finish(
                        run, status="failed", error_code=error, **counts.database_counts()
                    )
            result = {
                "run_id": str(run_id),
                "source": SOURCE,
                "area_name": requested_area,
                "area_code": expected_code,
                **result,
            }
            return (
                await publish_result(engine, result, observations=("population",))
                if _publish
                else result
            )
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_name}
            )
            await lock.commit()
