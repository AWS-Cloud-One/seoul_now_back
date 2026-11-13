"""P5 history and choices on isolated PostGIS; model/providers are synthetic."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from app.models import ChatAction, ChatRun, ChatRunCommit
from app.modules.chat_v2.api import create_app
from app.modules.chat_v2.contracts import RunRequest
from app.modules.chat_v2.runtime import ChatRuntime
from app.modules.chat_v2.storage import ChatV2Store
from app.modules.chat_v2.storage_contracts import StorageRejected
from tests.integration.test_chat_v2_dialogue import Harness
from tests.integration.test_chat_v2_lifecycle import request, run_request

pytestmark = pytest.mark.integration


def choose(option):
    return RunRequest(
        request_id=uuid4(),
        expected_condition_version=option.expected_condition_version,
        expected_state_version=option.expected_state_version,
        input={"type": "action", "action_id": option.action_id},
    )


def test_origin_and_headcount_choices_are_atomic_and_skip_model(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            asked = await h.say(
                "서울역에서 카페",
                {
                    "kind": "recommend",
                    "origin_query": "서울역",
                    "purposes": [{"purpose": "cafe"}],
                },
            )
            option = asked.question.options[0]
            assert asked.question.required_field == "origin" and option.label == "서울역"
            assert "decision" not in asked.question.model_dump_json()
            h.model.interpret.reset_mock()
            first, duplicate = await asyncio.gather(
                rt.submit(h.credential, h.conversation.conversation_id, choose(option)),
                rt.submit(h.credential, h.conversation.conversation_id, choose(option)),
            )
            assert first.identity == duplicate.identity
            await rt.tick()
            await asyncio.gather(*rt.jobs)
            done = await h.store.get_run(
                h.credential, h.conversation.conversation_id, first.identity.run_id
            )
            assert (
                done.response.recommendation and done.response.conditions.origin.label == "서울역"
            )
            h.model.interpret.assert_not_awaited()
            asked = await h.say(
                "인원 변경",
                {"kind": "needs_input", "required_field": "headcount", "question": "몇 명인가요?"},
            )
            option = next(o for o in asked.question.options if o.label == "3명")
            h.model.interpret.reset_mock()
            done = await run_request(h, rt, choose(option))
            assert done.response.conditions.headcount == 3
            h.model.interpret.assert_not_awaited()
            alias = choose(option)
            again = await rt.submit(h.credential, h.conversation.conversation_id, alias)
            assert again.identity == done.identity
            with pytest.raises(StorageRejected, match="REQUEST_ID_CONFLICT"):
                await rt.submit(
                    h.credential,
                    h.conversation.conversation_id,
                    alias.model_copy(
                        update={
                            "input": RunRequest.model_validate(
                                {
                                    **alias.model_dump(mode="json"),
                                    "input": {"type": "message", "text": "다른 내용"},
                                }
                            ).input
                        }
                    ),
                )
            _, events = await h.store.events(
                h.credential, h.conversation.conversation_id, asked.identity.run_id
            )
            assert [e.type for e in events][-2:] == ["clarification_required", "run_finished"]
            page = await h.store.history(h.credential, h.conversation.conversation_id)
            assert [
                t.user_message.text for t in page.items if t.user_message.input_type == "action"
            ] == ["서울역", "3명"]
            assert len(page.items) == 4
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 2
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_purpose_and_target_choices_preserve_pending_question(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            h.mode = "two"
            asked = await h.say("추천", {"kind": "recommend"})
            assert asked.question.required_field == "purposes"
            done = await run_request(
                h, rt, choose(next(o for o in asked.question.options if o.label == "카페"))
            )
            assert done.response.recommendation.selection_mode == "regional"
            await h.say(
                "인원 변경",
                {"kind": "needs_input", "required_field": "headcount", "question": "몇 명인가요?"},
            )
            asked = await h.say(
                "그곳 주차",
                {
                    "kind": "read",
                    "topics": ["parking"],
                    "targets": [{"kind": "name", "name": "모호한 장소"}],
                },
            )
            assert asked.question.required_field == "target" and len(asked.question.options) == 2
            assert all(o.target_ref and o.collection_id for o in asked.question.options)
            h.model.interpret.reset_mock()
            done = await run_request(h, rt, choose(asked.question.options[0]))
            assert done.response.lookup.items[0].topic == "parking"
            state = await h.current()
            assert state.pending_question.field == "headcount"
            assert state.pending_read is None
            assert state.questions[0].required_field == "headcount"
            assert all(o.status == "available" for o in state.questions[0].options)
            h.model.interpret.assert_not_awaited()
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_history_snapshot_paging_ownership_and_inflight_restore(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            for n in range(3):
                await h.say(
                    f"인원 {n}",
                    {
                        "kind": "needs_input",
                        "required_field": "headcount",
                        "question": "몇 명인가요?",
                    },
                )
            first = await h.store.history(h.credential, h.conversation.conversation_id, limit=2)
            assert [t.sequence for t in first.items] == [2, 3]
            pending_request = await request(h)
            accepted = await h.store.admit(
                h.credential,
                h.conversation.conversation_id,
                pending_request,
                dialogue=True,
                queued=True,
            )
            restored = ChatV2Store(h.writers)
            older = await restored.history(
                h.credential, h.conversation.conversation_id, limit=2, cursor=first.next_cursor
            )
            assert [t.sequence for t in older.items] == [1] and older.next_cursor is None
            latest = await restored.history(h.credential, h.conversation.conversation_id)
            assert latest.active_run_id == accepted.identity.run_id
            assert (
                latest.items[-1].run.status == "accepted" and latest.items[-1].run.response is None
            )
            replay = await h.store.admit(
                h.credential,
                h.conversation.conversation_id,
                pending_request,
                dialogue=True,
                queued=True,
            )
            assert replay.identity == accepted.identity
            assert (
                len((await restored.history(h.credential, h.conversation.conversation_id)).items)
                == 4
            )
            peer = await h.store.create_session()
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await restored.history(peer, h.conversation.conversation_id)
            other = await h.store.create_conversation(h.credential)
            with pytest.raises(StorageRejected, match="INVALID_CURSOR"):
                await restored.history(
                    h.credential, other.conversation_id, cursor=first.next_cursor
                )
            with pytest.raises(StorageRejected, match="INVALID_CURSOR"):
                await restored.history(h.credential, h.conversation.conversation_id, cursor="bad!")
            await h.store.cancel(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            final = await restored.history(h.credential, h.conversation.conversation_id)
            assert final.items[-1].run.response.status == "cancelled"
            assert final.items[-1].assistant_message_id == latest.items[-1].assistant_message_id
        finally:
            await h.close()

    asyncio.run(run())


def test_action_expiry_stale_version_refresh_and_cleanup(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            asked = await h.say(
                "인원",
                {"kind": "needs_input", "required_field": "headcount", "question": "몇 명인가요?"},
            )
            old = asked.question.options[0]
            now = h.store.clock()
            h.store.clock = lambda: now + timedelta(minutes=6)
            with pytest.raises(StorageRejected, match="ACTION_EXPIRED"):
                await h.store.admit(
                    h.credential,
                    h.conversation.conversation_id,
                    choose(old),
                    dialogue=True,
                    queued=True,
                )
            fresh = (await h.current()).questions[0].options[0]
            assert fresh.action_id != old.action_id and fresh.status == "available"
            await h.say("장소 찾아줘", {"kind": "search_places", "query": "카페"})
            with pytest.raises(StorageRejected, match="ACTION_STALE"):
                await h.store.admit(
                    h.credential,
                    h.conversation.conversation_id,
                    choose(fresh),
                    dialogue=True,
                    queued=True,
                )
            peer = await h.store.create_session()
            other = await h.store.create_conversation(peer)
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await h.store.admit(
                    peer, other.conversation_id, choose(fresh), dialogue=True, queued=True
                )
            await h.store.delete_conversation(h.credential, h.conversation.conversation_id)
            await h.store.cleanup()
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatAction)) == 0
        finally:
            await h.close()

    asyncio.run(run())


def test_interrupted_action_retry_does_not_call_model(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            asked = await h.say("추천", {"kind": "recommend"})
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, choose(asked.question.options[0])
            )
            claim, _, _ = await h.store.claim_next()
            assert (
                await h.store.get_run(
                    h.credential, h.conversation.conversation_id, accepted.identity.run_id
                )
            ).stage == "applying_action"
            async with h.writers.begin() as db:
                await db.execute(
                    update(ChatRun)
                    .where(ChatRun.run_id == accepted.identity.run_id)
                    .values(lease_expires_at=h.store.clock() - timedelta(seconds=1))
                )
            await h.store.recover()
            stopped = await h.store.get_run(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            assert stopped.retryable_tasks == ["action"]
            h.model.interpret.reset_mock()
            done = await run_request(
                h,
                rt,
                await request(
                    h,
                    retry={
                        "type": "retry",
                        "source_run_id": stopped.identity.run_id,
                        "task_ids": ["action"],
                    },
                ),
            )
            assert done.response.recommendation
            h.model.interpret.assert_not_awaited()
            with pytest.raises(StorageRejected):
                await h.store.checkpoint(claim, "saving")
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_http_history_question_and_action_contract(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        app = create_app()
        app.state.runtime = rt
        try:
            await h.start()
            asked = await h.say("추천", {"kind": "recommend"})
            headers = {
                "Authorization": f"Bearer {h.credential.session_id}."
                f"{h.credential.token.get_secret_value()}"
            }
            base = f"/api/v2/chat/conversations/{h.conversation.conversation_id}"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local", headers=headers
            ) as http:
                page = await http.get(base + "/messages?limit=1")
                assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
                question = page.json()["items"][0]["run"]["question"]
                assert question["clarification_id"] == asked.question.clarification_id
                body = choose(asked.question.options[0]).model_dump(mode="json")
                forged = {**body, "input": {**body["input"], "headcount": 99}}
                assert (await http.post(base + "/runs", json=forged)).status_code == 422
                accepted = await http.post(base + "/runs", json=body)
                assert accepted.status_code == 202
                await rt.tick()
                await asyncio.gather(*rt.jobs)
                result = (await http.get(accepted.json()["status_url"])).json()["run"]
                assert result["response"]["recommendation"]
                assert (await http.get(base + "/messages?limit=51")).status_code == 422
                assert (await http.get(base + "/messages?cursor=bad!")).status_code == 422
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())
