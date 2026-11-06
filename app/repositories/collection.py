"""Bounded collection summaries; no HTTP or provider-specific parsing."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.models import CollectionRejectionReason, CollectionRun, DataSource
from app.modules.data_pipeline.identity import PROCESSOR_VERSION

KEEP_RUNS = 20


class CollectionRepository:
    def __init__(self, session):
        self.session = session

    async def ensure_source(self, source):
        await self.session.execute(
            insert(DataSource)
            .values(
                source_code=source,
                display_name=source,
                provider_name="서울특별시"
                if source.startswith(("seoul_", "topis"))
                else "출처별 수집 자료",
            )
            .on_conflict_do_nothing()
        )

    async def start(self, source, target_key, *, run_id=None, started_at=None, kind="collection"):
        await self.ensure_source(source)
        run = CollectionRun(
            id=run_id or uuid4(),
            source=source,
            target_key=target_key,
            run_kind=kind,
            started_at=started_at or datetime.now(UTC),
            status="running",
            completeness="unknown",
            received=0,
            accepted=0,
            rejected=0,
            duplicates=0,
            processor_version=PROCESSOR_VERSION,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish(
        self,
        run,
        *,
        status,
        error_code=None,
        completeness="unknown",
        received=0,
        accepted=0,
        rejected=0,
        duplicates=0,
        rejection_reasons=None,
    ):
        """Persist a pipeline's outcome without deciding success or committing its transaction."""
        run.status = status
        run.error_code = error_code
        run.completeness = completeness
        run.received = received
        run.accepted = accepted
        run.rejected = rejected
        run.duplicates = duplicates
        await self.set_rejection_reasons(run.id, rejection_reasons or {})
        run.finished_at = datetime.now(UTC)
        await self.session.flush()
        return await self.prune_history(run.source)

    async def add_counts(
        self, run_id, *, received=0, accepted=0, rejected=0, duplicates=0, rejection_reasons=None
    ):
        """Commit a batch's counters in the same transaction as its typed writes."""
        run = await self.session.scalar(
            select(CollectionRun).where(CollectionRun.id == run_id).with_for_update()
        )
        if run is None or run.status != "running":
            raise ValueError("inactive_collection_cycle")
        for key, value in dict(
            received=received, accepted=accepted, rejected=rejected, duplicates=duplicates
        ).items():
            setattr(run, key, getattr(run, key) + value)
        for reason, count in (rejection_reasons or {}).items():
            if not count:
                continue
            stmt = insert(CollectionRejectionReason).values(
                run_id=run_id, reason_code=reason, occurrence_count=count
            )
            await self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["run_id", "reason_code"],
                    set_={"occurrence_count": CollectionRejectionReason.occurrence_count + count},
                )
            )
        await self.session.flush()

    async def prune_history(self, source):
        """Keep the latest finished summaries; current values/cursors never depend on them."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext("collection-history:" + source)))
        )
        expired = (
            select(CollectionRun.id)
            .where(CollectionRun.source == source, CollectionRun.status != "running")
            .order_by(CollectionRun.started_at.desc(), CollectionRun.id.desc())
            .offset(KEEP_RUNS)
        )
        ids = list(await self.session.scalars(expired))
        if not ids:
            return 0
        await self.session.execute(
            delete(CollectionRejectionReason).where(CollectionRejectionReason.run_id.in_(ids))
        )
        result = await self.session.execute(delete(CollectionRun).where(CollectionRun.id.in_(ids)))
        return result.rowcount

    async def recover_interrupted(self, source):
        """Caller holds the provider lease: close abandoned cycles from committed counters only."""
        runs = list(
            await self.session.scalars(
                select(CollectionRun)
                .where(CollectionRun.source == source, CollectionRun.status == "running")
                .with_for_update()
            )
        )
        for run in runs:
            run.status = "partial" if run.accepted else "failed"
            run.error_code = "collection_interrupted"
            run.completeness = "partial"
            run.finished_at = datetime.now(UTC)
        await self.session.flush()
        await self.prune_history(source)
        return len(runs)

    async def latest_run(self, source, target_key):
        return await self.session.scalar(
            select(CollectionRun)
            .where(CollectionRun.source == source, CollectionRun.target_key == target_key)
            .order_by(CollectionRun.started_at.desc(), CollectionRun.id.desc())
            .limit(1)
        )

    async def set_rejection_reasons(self, run_id, reasons):
        if not isinstance(reasons, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for key, count in reasons.items()
        ):
            raise ValueError("invalid_rejection_reasons")
        await self.session.execute(
            delete(CollectionRejectionReason).where(CollectionRejectionReason.run_id == run_id)
        )
        if reasons:
            rows = [
                dict(run_id=run_id, reason_code=key, occurrence_count=count)
                for key, count in reasons.items()
                if count
            ]
            if rows:
                await self.session.execute(insert(CollectionRejectionReason), rows)

    async def rejection_counts(self, run_ids):
        results = {key: {} for key in run_ids}
        if run_ids:
            rows = await self.session.scalars(
                select(CollectionRejectionReason).where(
                    CollectionRejectionReason.run_id.in_(run_ids)
                )
            )
            for row in rows:
                results[row.run_id][row.reason_code] = row.occurrence_count
        return results
