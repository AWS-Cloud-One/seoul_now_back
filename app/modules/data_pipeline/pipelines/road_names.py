"""Bounded name enrichment of existing geometry, followed by map publication."""

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.pipelines.publication import publish_result
from app.repositories.collection import CollectionRepository
from app.repositories.reference import ReferenceRepository
from app.repositories.road_reference import update_road_names
from jobs.shutdown import complete_cleanup

SOURCE = "topis_road_names"
STOP_ERRORS = {"provider_auth_error", "provider_quota_exceeded", "missing_topis_api_key"}


async def run_road_names(engine, fetch, *, limit=20, links=(), after=None, refresh=False):
    links = sorted(set(links))
    if (
        not 1 <= limit <= 200
        or len(links) > limit
        or (links and after is not None)
        or any(
            not re.fullmatch(r"[0-9]{10}", link) for link in [*links, *([after] if after else [])]
        )
    ):
        raise ValueError("invalid_road_name_selection")
    async with engine.connect() as lease:
        locked = await lease.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": SOURCE}
        )
        await lease.commit()
        if not locked:
            return {"source": SOURCE, "status": "skipped", "error_code": "already_running"}
        try:
            return await _collect(engine, fetch, limit, links, after, refresh)
        finally:

            async def release():
                try:
                    await lease.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": SOURCE}
                    )
                    await lease.commit()
                except BaseException:
                    await lease.invalidate()
                    raise

            await complete_cleanup(release())


async def _collect(engine, fetch, limit, links, after, refresh):
    started_at = datetime.now(UTC)
    factory = async_sessionmaker(engine)
    async with factory() as session:
        reference = ReferenceRepository(session)
        targets = await reference.road_name_links(
            limit=limit, links=links, after=after, refresh=refresh
        )
        if (links and set(targets) != set(links)) or not await reference.has_prerequisite(
            "topis_geometry"
        ):
            return {"source": SOURCE, "status": "failed", "error_code": "missing_road_geometry"}
    has_more, targets = len(targets) > limit, targets[:limit]
    records, failures, completed = [], [], []
    for link in targets:
        if completed:
            await asyncio.sleep(0.1)
        completed.append(link)
        try:
            record = await fetch(link)
            if record.get("link_id") != link or not isinstance(record.get("name"), str):
                raise CollectionError("invalid_road_name_response")
            name = record["name"].strip()
            if not name:
                raise CollectionError("invalid_road_name_response")
            records.append({"link_id": link, "name": name})
        except CollectionError as exc:
            error = str(exc)
            failures.append({"link_id": link, "error_code": error})
            if error in STOP_ERRORS:
                break
    async with factory.begin() as session:
        repo = CollectionRepository(session)
        run = await repo.start(SOURCE, "network:names", started_at=started_at)
        accepted, updated, missing = await session.run_sync(
            lambda sync: update_road_names(sync, records)
        )
        failures += [{"link_id": link, "error_code": "missing_road_geometry"} for link in missing]
        status = "partial" if failures and accepted else "failed" if failures else "success"
        await repo.finish(
            run,
            status=status,
            error_code=failures[0]["error_code"] if failures else None,
            completeness="complete" if len(completed) == len(targets) else "partial",
            received=len(completed),
            accepted=accepted,
            rejected=len(failures),
            rejection_reasons=Counter(item["error_code"] for item in failures),
        )
    return await publish_result(
        engine,
        {
            "source": SOURCE,
            "status": status,
            "scope": "batch",
            "records": accepted,
            "updated": updated,
            "completed": len(completed),
            "failed_links": failures,
            "has_more": has_more or len(completed) < len(targets),
            "next_after": completed[-1] if completed else after,
        },
        geometry=("roads",),
    )
