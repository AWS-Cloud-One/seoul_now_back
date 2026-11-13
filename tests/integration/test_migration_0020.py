"""P3 schema matches models and refuses to discard live lifecycle records."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import write_session_factory
from app.models import ChatRun
from app.modules.chat_v2.contracts import RunRequest
from app.modules.chat_v2.storage import ChatV2Store

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("normalized_database", ["0019"], indirect=True)
def test_lifecycle_upgrade_roundtrip_and_downgrade_guard(normalized_database):
    config = Config("alembic.ini")
    command.upgrade(config, "0020")
    command.downgrade(config, "0019")
    command.upgrade(config, "head")

    async def create_queued():
        engine = create_async_engine(normalized_database)
        try:
            store = ChatV2Store(write_session_factory(engine))
            credential = await store.create_session()
            conversation = await store.create_conversation(credential)
            await store.admit(
                credential,
                conversation.conversation_id,
                RunRequest(
                    request_id=uuid4(),
                    expected_condition_version=0,
                    expected_state_version=0,
                    input={"text": "카페"},
                ),
                dialogue=True,
                queued=True,
            )
            async with store.writers.begin() as db:
                await db.execute(update(ChatRun).values(implementation_version="p3-1"))
        finally:
            await engine.dispose()

    asyncio.run(create_queued())
    command.downgrade(config, "0020")
    with pytest.raises(DBAPIError, match="P3 runs must expire"):
        command.downgrade(config, "0019")
