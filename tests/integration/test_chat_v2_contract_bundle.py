"""Export real server wire examples from a disposable DB and synthetic providers."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.modules.chat_v2.api import create_app
from app.modules.chat_v2.http_contracts import ErrorEnvelope, RunPoll
from app.modules.chat_v2.runtime import ChatRuntime
from app.modules.chat_v2.storage_contracts import ConversationSnapshot, HistoryPage
from tests.integration.test_chat_v2_dialogue import Harness
from tests.integration.test_chat_v2_lifecycle import request

pytestmark = pytest.mark.integration


def test_server_examples_are_valid_public_contracts(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        samples = {}
        try:
            await h.start()

            async def capture(name, view):
                samples[name] = {
                    "snapshot": (await h.current()).model_dump(mode="json"),
                    "poll": RunPoll(run=view, events=[]).model_dump(mode="json"),
                    "history": (
                        await h.store.history(h.credential, h.conversation.conversation_id)
                    ).model_dump(mode="json"),
                }

            await capture("success", await h.first_recommendation())
            h.mode = "route_error"
            await capture(
                "partial",
                await h.say(
                    "주소랑 현재 이동시간",
                    {
                        "kind": "read",
                        "topics": ["place_info", "driving"],
                        "targets": [{"kind": "focus"}],
                    },
                ),
            )
            await capture(
                "question",
                await h.say(
                    "인원 변경",
                    {
                        "kind": "needs_input",
                        "required_field": "headcount",
                        "question": "몇 명인가요?",
                    },
                ),
            )
            pending = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            await capture(
                "cancelled",
                await h.store.cancel(
                    h.credential, h.conversation.conversation_id, pending.identity.run_id
                ),
            )
            h.store.clock = lambda: h.credential.expires_at + timedelta(seconds=1)
            app = create_app()
            app.state.runtime = rt  # No background worker/model; HTTP access check only.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http:
                expired = await http.get(
                    f"/api/v2/chat/conversations/{h.conversation.conversation_id}",
                    headers={
                        "Authorization": (
                            f"Bearer {h.credential.session_id}."
                            f"{h.credential.token.get_secret_value()}"
                        )
                    },
                )
            assert expired.status_code == 410
            samples["expired"] = {"http_status": 410, "body": expired.json()}
            ErrorEnvelope.model_validate(samples["expired"]["body"])
            for sample in (v for k, v in samples.items() if k != "expired"):
                ConversationSnapshot.model_validate(sample["snapshot"])
                RunPoll.model_validate(sample["poll"])
                HistoryPage.model_validate(sample["history"])
            assert samples["partial"]["poll"]["run"]["response"]["status"] == "partial"
            assert samples["question"]["poll"]["run"]["question"]["options"]
            assert samples["cancelled"]["poll"]["run"]["response"]["status"] == "cancelled"
            return samples
        finally:
            await rt.close()
            await h.close()

    samples = asyncio.run(run())
    if output := os.getenv("CHAT_CONTRACT_EXAMPLES_OUTPUT"):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The harness uses synthetic places only. Never export session tokens.
        path.write_text(
            json.dumps(
                {"source": "isolated-db-synthetic-providers", "cases": samples},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
