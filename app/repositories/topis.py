"""Bounded TOPIS cycle history and one latest attempt per road."""

from sqlalchemy import delete, exists, or_, select, text, union
from sqlalchemy.dialects.postgresql import insert

from app.models import (
    Base,
    CollectionRejectionReason,
    CollectionRun,
    PayloadFileDeletion,
    RawPayload,
    RoadLink,
    RoadTrafficAttemptLatest,
)

SOURCE = "topis_traffic"
KEEP_CYCLES = 20
CYCLE_TARGET = "network"
CYCLE_PART = "topis:cycle"
CHECKPOINT_PREFIX = "topis:checkpoint:"


async def write_attempt(session, run_id, entry):
    if not await session.scalar(select(exists().where(RoadLink.link_id == entry["link_id"]))):
        return
    statement = insert(RoadTrafficAttemptLatest).values(
        link_id=entry["link_id"],
        run_id=run_id,
        attempted_at=entry["received_at"],
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


async def preserve_legacy_attempts(session):
    """Old per-link log rows can be removed without losing current failure/no-record state."""
    await session.execute(
        text("""
        INSERT INTO road_traffic_attempt_latest
            (link_id,run_id,attempted_at,status,error_code,attempts,observation_updated)
        SELECT DISTINCT ON (l.link_id)
            l.link_id,r.id,COALESCE(r.finished_at,r.started_at),r.status,r.error_code,1,
            COALESCE(p.run_id=r.id,false)
        FROM collection_runs r JOIN road_links l ON r.target_key='link:' || l.link_id
        LEFT JOIN road_traffic_latest t ON t.link_id=l.link_id AND t.source='topis_traffic'
        LEFT JOIN raw_payloads p ON p.payload_id=t.payload_id
        WHERE r.source='topis_traffic' AND r.status <> 'running'
            AND r.run_kind IN ('collection','legacy')
        ORDER BY l.link_id,r.started_at DESC,r.id DESC
        ON CONFLICT (link_id) DO UPDATE SET
            run_id=EXCLUDED.run_id,attempted_at=EXCLUDED.attempted_at,status=EXCLUDED.status,
            error_code=EXCLUDED.error_code,attempts=EXCLUDED.attempts,
            observation_updated=EXCLUDED.observation_updated
        WHERE road_traffic_attempt_latest.attempted_at < EXCLUDED.attempted_at
    """)
    )


async def prune_history(session):
    """Queue files and delete metadata atomically; referenced evidence survives detached."""
    await preserve_legacy_attempts(session)
    keep = (
        select(CollectionRun.id)
        .where(CollectionRun.source == SOURCE, CollectionRun.target_key == CYCLE_TARGET)
        .order_by(CollectionRun.started_at.desc(), CollectionRun.id.desc())
        .limit(KEEP_CYCLES)
    )
    expired = select(CollectionRun.id).where(
        CollectionRun.source == SOURCE,
        CollectionRun.id.not_in(keep),
        or_(CollectionRun.target_key != CYCLE_TARGET, CollectionRun.status != "running"),
    )
    finished = select(CollectionRun.id).where(CollectionRun.status != "running")
    candidate = (RawPayload.source == SOURCE) & or_(
        RawPayload.run_id.in_(expired),
        RawPayload.run_id.is_(None),
        (RawPayload.part_key.startswith(CHECKPOINT_PREFIX) | (RawPayload.part_key == "topis:start"))
        & RawPayload.run_id.in_(finished),
    )
    # Every typed consumer and every retained child's ancestry protects its evidence.
    roots = []
    for table in Base.metadata.tables.values():
        if table.name == RawPayload.__tablename__:
            continue
        for foreign_key in table.foreign_keys:
            if foreign_key.target_fullname == "raw_payloads.payload_id":
                column = foreign_key.parent
                roots.append(select(column.label("payload_id")).where(column.is_not(None)))
    roots.append(
        select(RawPayload.parent_payload_id.label("payload_id")).where(
            candidate.is_not(True),
            RawPayload.parent_payload_id.is_not(None),
        )
    )
    root_ids = union(*roots).subquery()
    protected = (
        select(RawPayload.payload_id, RawPayload.parent_payload_id)
        .where(RawPayload.payload_id.in_(select(root_ids.c.payload_id)))
        .cte("protected_topis_evidence", recursive=True)
    )
    protected = protected.union(
        select(RawPayload.payload_id, RawPayload.parent_payload_id).join(
            protected, RawPayload.payload_id == protected.c.parent_payload_id
        )
    )
    removable = select(RawPayload.payload_id).where(
        candidate,
        RawPayload.payload_id.not_in(select(protected.c.payload_id)),
    )
    # The queue survives process death between DB commit and filesystem cleanup.
    await session.execute(
        insert(PayloadFileDeletion)
        .from_select(
            ["file_ref"],
            select(RawPayload.file_ref).where(RawPayload.payload_id.in_(removable)).distinct(),
        )
        .on_conflict_do_nothing()
    )
    removed_payloads = await session.execute(
        delete(RawPayload).where(RawPayload.payload_id.in_(removable))
    )
    await session.execute(
        delete(CollectionRejectionReason).where(CollectionRejectionReason.run_id.in_(expired))
    )
    removed_runs = await session.execute(delete(CollectionRun).where(CollectionRun.id.in_(expired)))
    return {"runs_removed": removed_runs.rowcount, "payloads_removed": removed_payloads.rowcount}
