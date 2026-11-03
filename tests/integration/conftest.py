"""Read stored evidence directly for collection assertions; no inspection API."""

import pytest
from sqlalchemy import func, select

from app.models import CollectionRun, RawPayload
from app.storage.payloads import configured_store


@pytest.fixture
def stored_payload():
    async def read(session, source, target_key):
        stamp = func.coalesce(
            RawPayload.received_at, RawPayload.legacy_collected_at, RawPayload.stored_at
        )
        row = await session.scalar(
            select(RawPayload)
            .join(CollectionRun)
            .where(
                CollectionRun.source == source,
                CollectionRun.target_key == target_key,
                CollectionRun.status.in_(["success", "partial"]),
                RawPayload.format == "json",
                RawPayload.document_type.in_(["object", "array"]),
                RawPayload.payload_kind != "legacy_observation",
            )
            .order_by(stamp.desc(), RawPayload.stored_at.desc())
            .limit(1)
        )
        assert row is not None, (source, target_key)
        return row, configured_store().load_json(row)

    return read
