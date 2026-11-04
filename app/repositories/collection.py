"""Collection attempts and immutable evidence; no HTTP or provider-specific parsing."""

import asyncio
import hashlib
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.models import CollectionRejectionReason, CollectionRun, DataSource, RawPayload
from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.storage.payloads import configured_store
from app.storage.redaction import redact


async def lock_payload_file(session, sha256):
    # Writers and post-commit cleanup use the same lock, including identical content
    # shared by different providers. A collector must lock before creating the file.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("payload-file:" + sha256)))
    )


class CollectionRepository:
    def __init__(self, session):
        self.session = session
        self.store = configured_store()

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

    async def archive(
        self, run_id, data, received_at, *, kind="provider_response", part="response", legacy=None
    ):
        data = redact(data)
        body = self.store.encode_json(data)
        await lock_payload_file(self.session, hashlib.sha256(body).hexdigest())
        stored = await asyncio.to_thread(self.store.put_bytes, body)
        run = await self.session.get(CollectionRun, run_id)
        document_type = (
            "object" if isinstance(data, dict) else "array" if isinstance(data, list) else "scalar"
        )
        payload = RawPayload(
            payload_id=uuid4(),
            run_id=run_id,
            source=run.source,
            part_key=part,
            format="json",
            payload_kind=kind,
            file_ref=stored.file_ref,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            document_type=document_type,
            received_at=received_at,
            **(legacy or {}),
        )
        self.session.add(payload)
        await self.session.flush()
        return payload

    async def archive_file(self, run_id, body, *, part, metadata):
        """Preserve the exact official bytes used for parsing, without inventing receipt time."""
        await lock_payload_file(self.session, hashlib.sha256(body).hexdigest())
        stored = await asyncio.to_thread(self.store.put_bytes, body)
        run = await self.session.get(CollectionRun, run_id)
        if stored.sha256 != metadata["sha256"] or stored.byte_size != metadata["byte_size"]:
            raise ValueError("source_file_evidence_mismatch")
        payload = RawPayload(
            payload_id=uuid4(),
            run_id=run_id,
            source=run.source,
            part_key=part,
            payload_kind="source_file",
            format="xlsx",
            document_type="binary",
            file_ref=stored.file_ref,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            received_at=None,
            source_version=metadata["version"],
            reference_date=date.fromisoformat(metadata["reference_date"])
            if metadata.get("reference_date")
            else None,
        )
        self.session.add(payload)
        await self.session.flush()
        return payload

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
