"""Current typed TOPIS values and attempts, independent of bounded cycle logs."""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.models import (
    CollectionRejectionReason,
    CollectionRun,
    RoadLink,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.repositories.collection import KEEP_RUNS, CollectionRepository

SOURCE = "topis_traffic"
KEEP_CYCLES = KEEP_RUNS
CYCLE_TARGET = "network"


def write_observation(session, link, values):
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("traffic:topis_traffic:" + link)))
    )
    table = RoadTrafficLatest.__table__
    stmt = insert(table).values(
        source=SOURCE,
        link_id=link,
        processed_at=datetime.now(UTC),
        processor_version=PROCESSOR_VERSION,
        **values,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "link_id"],
        set_={key: stmt.excluded[key] for key in (*values, "processed_at", "processor_version")},
        where=and_(
            or_(table.c.received_at.is_(None), table.c.received_at <= values["received_at"]),
            table.c.observed_at.is_(None),
        ),
    )
    return session.scalar(stmt.returning(table.c.link_id)) is not None


async def has_active_road(session, link):
    return bool(
        await session.scalar(
            select(
                exists().where(
                    RoadLink.link_id == link,
                    RoadLink.source == "topis_geometry",
                    RoadLink.is_active.is_(True),
                )
            )
        )
    )


async def write_attempt(session, run_id, entry):
    if not await session.scalar(select(exists().where(RoadLink.link_id == entry["link_id"]))):
        return
    statement = insert(RoadTrafficAttemptLatest).values(
        link_id=entry["link_id"],
        run_id=run_id,
        attempted_at=entry["attempted_at"],
        status=entry["status"],
        error_code=entry["error_code"],
        attempts=entry["attempts"],
        observation_updated=entry.get("processing", {}).get("updated", False),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["link_id"],
            set_={
                key: getattr(statement.excluded, key)
                for key in (
                    "run_id",
                    "attempted_at",
                    "status",
                    "error_code",
                    "attempts",
                    "observation_updated",
                )
            },
            where=RoadTrafficAttemptLatest.attempted_at <= statement.excluded.attempted_at,
        )
    )


async def record_batch_counts(session, run_id, entries):
    """Caller commits observations, attempts and these aggregate counts together."""
    run = await session.scalar(
        select(CollectionRun).where(CollectionRun.id == run_id).with_for_update()
    )
    if run is None or run.status != "running":
        raise ValueError("inactive_topis_cycle")
    run.received += len(entries)
    run.accepted += sum(entry["processing"]["updated"] for entry in entries)
    run.rejected = run.received - run.accepted
    reasons = Counter(entry["error_code"] for entry in entries if entry["error_code"])
    for reason, count in reasons.items():
        stmt = insert(CollectionRejectionReason).values(
            run_id=run_id,
            reason_code=reason,
            occurrence_count=count,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["run_id", "reason_code"],
                set_={"occurrence_count": CollectionRejectionReason.occurrence_count + count},
            )
        )
    await session.flush()


async def prune_history(session):
    """Delete old log summaries only; current observations/attempts survive via nullable run IDs."""
    return {"runs_removed": await CollectionRepository(session).prune_history(SOURCE)}
