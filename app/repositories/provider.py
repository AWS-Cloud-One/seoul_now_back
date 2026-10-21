from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from app.models.provider import ProviderSnapshot


async def save_snapshot(session, source, data):
    statement = insert(ProviderSnapshot).values(
        source=source, data=data, collected_at=datetime.now(UTC)
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ProviderSnapshot.source],
            set_={"data": statement.excluded.data, "collected_at": statement.excluded.collected_at},
            where=ProviderSnapshot.collected_at < statement.excluded.collected_at,
        )
    )
