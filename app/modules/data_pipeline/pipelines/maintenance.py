"""Close interrupted summary logs under the same lease used by the collector."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.repositories.collection import CollectionRepository


async def recover_collection(engine, source):
    async with engine.connect() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": source}
        )
        await lock.commit()
        if not acquired:
            return 0
        try:
            async with async_sessionmaker(engine).begin() as session:
                return await CollectionRepository(session).recover_interrupted(source)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": source})
            await lock.commit()
