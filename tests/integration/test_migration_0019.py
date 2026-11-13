"""Preserve prior V2 result identities and refuse lossy downgrade."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models import ChatResult, ChatRun
from tests.integration.test_chat_v2_dialogue import Harness

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("normalized_database", ["0018"], indirect=True)
def test_result_identity_upgrade_matches_metadata(normalized_database):
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)

    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            routed = await h.first_recommendation()
            result_id = routed.response.recommendation.result_id
            # Routed results have legacy rows; upgrading restores their V2 identities.
            # Emulate pre-P3 rows for this historical 0019 migration fixture.
            async with h.writers.begin() as db:
                await db.execute(update(ChatRun).values(implementation_version="p2"))
            await asyncio.to_thread(command.downgrade, config, "0018")
            await asyncio.to_thread(command.upgrade, config, "head")
            async with h.writers() as db:
                identity = await db.scalar(
                    select(ChatResult).where(ChatResult.result_id == result_id)
                )
                assert identity.run_id == routed.identity.run_id
            saved = await h.store.get_run(
                h.credential, h.conversation.conversation_id, routed.identity.run_id
            )
            assert saved.response == routed.response
            # A new regional result has no invented legacy route. Downgrade must refuse it.
            h.conversation = await h.store.create_conversation(h.credential)
            regional = await h.say(
                "카페 추천", {"kind": "recommend", "purposes": [{"purpose": "cafe"}]}
            )
            assert regional.response.recommendation.selection_mode == "regional"
            async with h.writers.begin() as db:
                await db.execute(update(ChatRun).values(implementation_version="p2"))
            with pytest.raises(IntegrityError):
                await asyncio.to_thread(command.downgrade, config, "0018")
            await asyncio.to_thread(command.check, config)
        finally:
            await h.close()

    asyncio.run(run())
