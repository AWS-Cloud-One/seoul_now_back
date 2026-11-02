from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from app.models.parking import CollectionRun
from app.models.provider import ProviderSnapshot
from app.repositories.external_api import archive_response


def snapshot_outcome(data):
    """A capped dataset is usable, but must not be reported as a complete collection."""
    datasets = data.get("datasets")
    if isinstance(datasets, dict) and any(
        isinstance(dataset, dict) and dataset.get("complete") is False
        for dataset in datasets.values()
    ):
        return "partial"
    return "success"


async def save_snapshot(session, source, data):
    collected_at = datetime.now(UTC)
    statement = insert(ProviderSnapshot).values(source=source, data=data, collected_at=collected_at)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ProviderSnapshot.source],
            set_={"data": statement.excluded.data, "collected_at": statement.excluded.collected_at},
            where=ProviderSnapshot.collected_at < statement.excluded.collected_at,
        )
    )
    # Failed attempts retain their own outcome without replacing the last good payload.
    attempt_source = None
    if source.endswith(":attempt"):
        attempt_source = source.removesuffix(":attempt")
    elif source.startswith("topis_attempt:"):
        attempt_source = source.replace("topis_attempt:", "topis:", 1)
    if attempt_source is not None:
        if data.get("status") == "failed":
            session.add(
                CollectionRun(
                    id=uuid4(),
                    source=attempt_source[:80],
                    started_at=collected_at,
                    finished_at=collected_at,
                    status="failed",
                    received=0,
                    accepted=0,
                    rejected=0,
                    duplicates=0,
                    rejection_reasons={},
                    error_code=str(data.get("error_code") or "provider_error")[:80],
                )
            )
        return
    if source.endswith("_status"):
        return
    run_id = uuid4()
    session.add(
        CollectionRun(
            id=run_id,
            source=source[:80],
            started_at=collected_at,
            finished_at=collected_at,
            status=snapshot_outcome(data),
            received=1,
            accepted=1,
            rejected=0,
            duplicates=0,
            rejection_reasons={},
        )
    )
    await session.flush()
    await archive_response(
        session,
        run_id,
        source[:80],
        "provider_snapshot",
        data,
        collected_at,
        [
            {
                "record_key": source,
                "name": None,
                "observed_at": None,
                "latitude": None,
                "longitude": None,
                "data": data,
            }
        ],
    )
