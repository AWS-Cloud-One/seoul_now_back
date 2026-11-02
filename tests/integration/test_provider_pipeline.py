import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.models.external_api import ExternalApiObservation, ExternalApiResponse
from app.models.parking import CollectionRun
from app.models.provider import ProviderSnapshot
from app.repositories.provider import save_snapshot


@pytest.mark.integration
def test_provider_history_and_failure_attempt_preserve_latest():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")

    async def run():
        engine = create_engine(Settings(_env_file=None, database_url=url))
        factory = async_sessionmaker(engine)
        source = "test_" + uuid4().hex
        try:
            async with factory.begin() as session:
                await save_snapshot(session, source, {"items": [{"id": "1", "count": 1}]})
                await save_snapshot(session, source + ":attempt", {"status": "success"})
            async with factory.begin() as session:
                await save_snapshot(
                    session,
                    source,
                    {
                        "items": [{"id": "1", "count": 2}],
                        "datasets": {"parking": {"rows": [], "complete": False}},
                    },
                )
            async with factory.begin() as session:
                await save_snapshot(
                    session,
                    source + ":attempt",
                    {"status": "failed", "error_code": "provider_network_error"},
                )
            async with factory() as session:
                latest = await session.get(ProviderSnapshot, source)
                assert latest.data["items"][0]["count"] == 2
                attempt = await session.get(ProviderSnapshot, source + ":attempt")
                assert attempt.data["status"] == "failed"
                history = list(
                    await session.scalars(
                        select(ExternalApiResponse).where(ExternalApiResponse.source == source)
                    )
                )
                assert len(history) == 2
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.source == source)
                    )
                )
                assert sorted(r.status for r in runs) == ["failed", "partial", "success"]
        finally:
            async with factory.begin() as session:
                responses = select(ExternalApiResponse.id).where(
                    ExternalApiResponse.source == source
                )
                await session.execute(
                    delete(ExternalApiObservation).where(
                        ExternalApiObservation.response_id.in_(responses)
                    )
                )
                await session.execute(
                    delete(ExternalApiResponse).where(ExternalApiResponse.source == source)
                )
                await session.execute(delete(CollectionRun).where(CollectionRun.source == source))
                await session.execute(
                    delete(ProviderSnapshot).where(
                        ProviderSnapshot.source.in_([source, source + ":attempt"])
                    )
                )
            await engine.dispose()

    asyncio.run(run())
