"""Ordered history backfills old runs without changing their stored responses."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.models import ChatRun
from tests.integration.test_chat_v2_dialogue import Harness
from tests.integration.test_chat_v2_lifecycle import request

pytestmark = pytest.mark.integration


def test_history_upgrade_backfill_and_downgrade_guard(normalized_database):
    config = Config("alembic.ini")
    command.check(config)

    async def seed():
        h = Harness(normalized_database)
        try:
            await h.start()
            await h.say("카페", {"kind": "recommend", "purposes": [{"purpose": "cafe"}]})
            async with h.writers.begin() as db:
                await db.execute(update(ChatRun).values(implementation_version="p4-1"))
                return (
                    h.credential,
                    h.conversation.conversation_id,
                    await db.scalar(select(ChatRun.response)),
                )
        finally:
            await h.close()

    credential, conversation_id, response = asyncio.run(seed())
    command.downgrade(config, "0021")
    command.upgrade(config, "head")
    command.check(config)

    async def verify():
        h = Harness(normalized_database)
        h.credential = credential
        try:
            page = await h.store.history(credential, conversation_id)
            assert len(page.items) == 1 and page.items[0].sequence == 1
            assert page.items[0].run.response.model_dump(mode="json") == response
            assert page.items[0].user_message.text == "카페"
            h.conversation = await h.store.conversation(credential, conversation_id)
            await h.store.admit(
                credential, conversation_id, await request(h), dialogue=True, queued=True
            )
        finally:
            await h.close()

    asyncio.run(verify())
    with pytest.raises(DBAPIError, match="P5 runs/actions must expire"):
        command.downgrade(config, "0021")
    command.check(config)
