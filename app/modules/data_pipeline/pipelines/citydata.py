"""Collect current CITYDATA fields with one bounded summary per collection cycle."""

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
from app.modules.data_pipeline.pipelines.parking import CityDataParkingBatch, save_citydata_parking
from app.modules.data_pipeline.pipelines.publication import publication_summary, publish_result
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.population import PopulationWriter

SOURCE = "seoul_citydata"
STOP_ERRORS = {"provider_auth_error", "provider_quota_exceeded", "missing_citydata_api_key"}


async def run_citydata_batch(engine, fetch_area, targets, *, report=None):
    return await _run_cycle(engine, fetch_area, targets, report=report)


async def run_citydata(engine, fetch, requested_area, *, expected_code):
    requested_area, expected_code = requested_area.strip(), expected_code.strip()
    if not requested_area or not expected_code:
        raise CollectionError("missing_area_identity")
    return await _run_cycle(
        engine, lambda code, name: fetch(), {expected_code: requested_area}, single=True
    )


async def _collect_area(factory, run_id, fetch, code, name):
    started = datetime.now(UTC)
    try:
        try:
            data = await fetch()
        except CityDataResponseError as exc:
            data = exc.document
        document = (
            data
            if isinstance(data, CityDataDocument)
            else CityDataDocument(data, datetime.now(UTC), require_envelope=False)
        )
        async with factory.begin() as session:
            run = await session.get(CollectionRun, run_id)
            result, parking = await store_citydata(
                session,
                run,
                document.body,
                document.received_at,
                expected_code=code,
                expected_name=name,
                require_envelope=document.require_envelope,
                attempted_at=started,
            )
    except Exception as exc:
        error = str(exc) if isinstance(exc, (CollectionError, CityDataError)) else "internal_error"
        parking = CityDataParkingBatch(section_issues=1, sections=1)
        result = {
            "status": "failed",
            "error_code": error,
            **ProcessingCounts(unit="section", scope="citydata_section_processing").as_dict(),
            "sections": {},
        }
        async with factory.begin() as session:
            await session.run_sync(
                lambda sync: PopulationWriter(sync).attempt(code, started, "failed", error)
            )
            await CollectionRepository(session).add_counts(run_id, rejection_reasons={error: 1})
    return {
        "run_id": str(run_id),
        "source": SOURCE,
        "area_name": name,
        "area_code": code,
        **result,
    }, parking


async def _finish_cycle(factory, run_id, status, error, *, complete):
    async with factory.begin() as session:
        repo = CollectionRepository(session)
        run = await session.get(CollectionRun, run_id)
        reasons = (await repo.rejection_counts([run_id]))[run_id]
        counts = ProcessingCounts(
            unit="record",
            scope="citydata_population_and_parking_storage",
            received=run.received,
            accepted=run.accepted,
            rejected=run.rejected,
            duplicates=run.duplicates,
            rejection_reasons=reasons,
        )
        await repo.finish(
            run,
            status="partial"
            if status == "paused_provider_limit" and run.accepted
            else "failed"
            if status == "paused_provider_limit"
            else status,
            error_code=error,
            completeness="complete" if complete and status == "success" else "partial",
            **counts.database_counts(),
        )
        return counts.as_dict()


async def _run_cycle(engine, fetch_area, targets, *, report=None, single=False):
    if not targets:
        raise ValueError("empty_citydata_targets")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Single-area/manual and full-network jobs share one lease; neither can overwrite
    # another cycle's latest attempt or interleave parking candidates.
    async with engine.connect() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": SOURCE}
        )
        await lock.commit()
        if not acquired:
            return {"status": "skipped", "error_code": "already_running"}
        run_id = uuid4()
        try:
            async with factory.begin() as session:
                repo = CollectionRepository(session)
                await repo.recover_interrupted(SOURCE)
                await repo.start(
                    SOURCE, "area:" + next(iter(targets)) if single else "all", run_id=run_id
                )
            counts, parking, stop_reason = Counter(), CityDataParkingBatch(), None
            first_error = None
            for index, (code, name) in enumerate(targets.items()):
                area_result, selected = await _collect_area(
                    factory, run_id, lambda code=code, name=name: fetch_area(code, name), code, name
                )
                first_error = first_error or area_result.get("error_code")
                parking.extend(selected)
                counts[area_result["status"]] += 1
                if report:
                    report(
                        {
                            key: area_result.get(key)
                            for key in ("area_code", "area_name", "status", "error_code")
                        }
                    )
                if area_result.get("error_code") in STOP_ERRORS:
                    stop_reason = area_result["error_code"]
                    break
                if index + 1 < len(targets):
                    await asyncio.sleep(0.25)
            try:
                stored = await save_citydata_parking(engine, parking, run_id=run_id)
            except Exception:
                stored = {"status": "failed", "error_code": "parking_storage_failed"}
            statuses = set(counts) | {stored["status"]}
            status = (
                "failed"
                if "failed" in statuses
                else "success"
                if statuses == {"success"}
                else "partial"
            )
            if stop_reason:
                status = "paused_provider_limit"
            error = stop_reason or stored.get("error_code") or first_error
            # Preserve a useful reason if an earlier area failed and the last one succeeded.
            processing = await _finish_cycle(
                factory, run_id, status, error, complete=not stop_reason
            )
            result = {
                **(area_result if single else {}),
                "run_id": str(run_id),
                "source": SOURCE,
                "cycle_complete": stop_reason is None,
                "status": status,
                "error_code": error,
                "stop_reason": stop_reason,
                "completed": sum(counts.values()),
                "remaining": len(targets) - sum(counts.values()),
                "total": len(targets),
                **{key: counts[key] for key in ("success", "partial", "failed", "skipped")},
                "processing": processing,
                "parking_storage": stored,
            }
            result = await publish_result(engine, result, observations=("population",))
            result["publication"] = publication_summary(
                {
                    **result["publication"]["layers"],
                    **stored.get("publication", {}).get("layers", {}),
                }
            )
            return result
        except BaseException:
            # Do not replay pending inputs after an uncertain commit or interruption.
            # Previously committed population/parking rows and their counters remain.
            async with factory.begin() as session:
                await CollectionRepository(session).recover_interrupted(SOURCE)
            raise
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": SOURCE})
            await lock.commit()
