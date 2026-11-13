"""P3 boundaries on real isolated PostGIS, synthetic model and provider only."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from app.models import ChatConversation, ChatRun, ChatRunCommit, ChatSession, Place
from app.modules.chat_v2.api import create_app
from app.modules.chat_v2.contracts import DialogueProposal, RunRequest
from app.modules.chat_v2.dialogue_context import ReadItem
from app.modules.chat_v2.finalization import DatabaseFinalizer
from app.modules.chat_v2.runtime import ChatRuntime
from app.modules.chat_v2.storage_contracts import StorageRejected
from tests.integration.test_chat_v2_dialogue import Harness

pytestmark = pytest.mark.integration


async def request(h, text="카페 추천", *, retry=None):
    current = await h.current()
    return RunRequest(
        request_id=uuid4(),
        expected_condition_version=current.condition_version,
        expected_state_version=current.state_version,
        input=retry or {"text": text},
    )


def recommend(h):
    h.model.interpret.return_value = DialogueProposal(
        decision={"kind": "recommend", "purposes": [{"purpose": "cafe"}], "evidence_text": "카페"}
    )


async def run_request(h, rt, req):
    accepted = await rt.submit(h.credential, h.conversation.conversation_id, req)
    await rt.tick()
    await asyncio.gather(*rt.jobs)
    return await h.store.get_run(
        h.credential, h.conversation.conversation_id, accepted.identity.run_id
    )


def test_http_admission_replay_cancel_and_ownership(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution, poll_seconds=0.01, heartbeat_seconds=0.02)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args):
            entered.set()
            await release.wait()
            return DialogueProposal(
                decision={
                    "kind": "recommend",
                    "purposes": [{"purpose": "cafe"}],
                    "evidence_text": "카페",
                }
            )

        @asynccontextmanager
        async def factory():
            yield rt

        app = create_app(factory)
        try:
            await h.start()
            h.model.interpret.side_effect = slow
            headers = {
                "Authorization": f"Bearer {h.credential.session_id}."
                f"{h.credential.token.get_secret_value()}"
            }
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://local"
                ) as http,
            ):
                req = await request(h)
                url = f"/api/v2/chat/conversations/{h.conversation.conversation_id}/runs"
                first = await http.post(url, json=req.model_dump(mode="json"), headers=headers)
                assert first.status_code == 202, first.text
                assert first.headers["cache-control"] == "no-store"
                status_url = first.json()["status_url"]
                await asyncio.wait_for(entered.wait(), 3)
                again = await http.post(url, json=req.model_dump(mode="json"), headers=headers)
                assert again.json()["status_url"] == status_url
                changed = req.model_copy(deep=True)
                changed.input.text = "다른 내용"
                assert (
                    await http.post(url, json=changed.model_dump(mode="json"), headers=headers)
                ).status_code == 409
                peer = await http.post("/api/v2/chat/sessions")
                peer_header = {"Authorization": f"Bearer {peer.json()['session_token']}"}
                assert (await http.get(status_url, headers=peer_header)).status_code == 404
                assert (
                    await http.post(
                        status_url + "/cancel",
                        json={"request_id": str(uuid4())},
                        headers=peer_header,
                    )
                ).status_code == 404
                cancelled = await http.post(
                    status_url + "/cancel", json={"request_id": str(uuid4())}, headers=headers
                )
                assert cancelled.json()["response"]["status"] == "cancelled"
                release.set()
                await asyncio.gather(*rt.jobs)
                snapshot = (await http.get(status_url, headers=headers)).json()
                assert snapshot["run"]["response"]["status"] == "cancelled"
                seqs = [event["event_seq"] for event in snapshot["events"]]
                assert seqs == list(range(1, len(seqs) + 1))
                assert not any(e["type"].endswith("committed") for e in snapshot["events"])
                assert (
                    await http.get(status_url + f"?after_seq={seqs[-1]}", headers=headers)
                ).json()["events"] == []
                assert h.model.interpret.await_count == 1
                assert (await h.current()).conditions.purposes == []
        finally:
            release.set()
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_accepted_survives_process_loss_and_only_one_worker_claims(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            recommend(h)
            req = await request(h)
            admitted = await rt.submit(h.credential, h.conversation.conversation_id, req)
            claims = await asyncio.gather(h.store.claim_next(), h.store.claim_next())
            assert sum(c is not None for c in claims) == 1
            claim, original, source = next(c for c in claims if c is not None)
            await rt._execute(claim, original, source)
            replay = await rt.submit(h.credential, h.conversation.conversation_id, req)
            assert replay.identity == admitted.identity and replay.response.recommendation
            assert h.model.interpret.await_count == 1
            finished = await h.store.cancel(
                h.credential, h.conversation.conversation_id, admitted.identity.run_id
            )
            assert finished == replay
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 1
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_lease_loss_fences_old_worker_and_explicit_retry(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            recommend(h)
            admitted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            old_claim, _, _ = await h.store.claim_next()
            async with h.writers.begin() as db:
                await db.execute(
                    update(ChatRun).values(lease_expires_at=h.store.clock() - timedelta(seconds=1))
                )
            assert await h.store.recover() == 1
            stopped = await h.store.get_run(
                h.credential, h.conversation.conversation_id, admitted.identity.run_id
            )
            assert stopped.response.status == "interrupted" and stopped.retryable_tasks == [
                "interpretation"
            ]
            assert h.model.interpret.await_count == 0
            with pytest.raises(StorageRejected):
                await h.store.checkpoint(old_claim, "recommending")
            retried = await run_request(
                h,
                rt,
                await request(
                    h,
                    retry={
                        "type": "retry",
                        "source_run_id": admitted.identity.run_id,
                        "task_ids": ["interpretation"],
                    },
                ),
            )
            assert retried.response.recommendation and h.model.interpret.await_count == 1
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_cancel_after_preparation_blocks_commit(normalized_database, monkeypatch):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        entered, release = asyncio.Event(), asyncio.Event()
        original = DatabaseFinalizer._commit

        async def delayed(self, prepared):
            entered.set()
            await release.wait()
            return await original(self, prepared)

        monkeypatch.setattr(DatabaseFinalizer, "_commit", delayed)
        try:
            await h.start()
            recommend(h)
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            await rt.tick()
            await asyncio.wait_for(entered.wait(), 3)
            cancelled = await h.store.cancel(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            assert cancelled.response.status == "cancelled"
            release.set()
            await asyncio.gather(*rt.jobs)
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 0
            assert (await h.current()).condition_version == 0
        finally:
            release.set()
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_recommendation_retry_keeps_committed_conditions_and_skips_model(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            recommend(h)
            h.mode = "search_error"
            failed = await run_request(h, rt, await request(h))
            assert failed.response.snapshot.finalization.commit.state == "committed"
            assert failed.retryable_tasks == ["recommendation"]
            version = (await h.current()).condition_version
            h.mode = "ok"
            done = await run_request(
                h,
                rt,
                await request(
                    h,
                    retry={
                        "type": "retry",
                        "source_run_id": failed.identity.run_id,
                        "task_ids": ["recommendation"],
                    },
                ),
            )
            assert done.response.recommendation, done.model_dump()
            assert done.response.condition_version == version
            assert done.response.snapshot.finalization.commit.receipt.changed_fields == []
            assert h.model.interpret.await_count == 1
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_lookup_retry_only_failed_item(normalized_database, monkeypatch):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        calls = []
        fail = True
        original_read = h.execution.tools._read_one

        async def read_one(topic, target, context):
            calls.append(topic)
            if topic != "parking" or not fail:
                return await original_read(topic, target, context)
            return ReadItem(
                topic=topic,
                place_ref=target.ref_id,
                status="error" if topic == "parking" and fail else "ok",
                reason_code="PROVIDER_TIMEOUT" if topic == "parking" and fail else None,
                retryable=topic == "parking" and fail,
            )

        try:
            await h.start()
            recommend(h)
            first = await run_request(h, rt, await request(h))
            monkeypatch.setattr(h.execution.tools, "_read_one", read_one)
            h.model.interpret.return_value = DialogueProposal(
                decision={
                    "kind": "read",
                    "topics": ["place_info", "parking"],
                    "targets": [{"kind": "focus"}],
                    "evidence_text": "정보",
                }
            )
            asked = await run_request(h, rt, await request(h, "정보"))
            assert first.response.recommendation and asked.retryable_tasks == ["lookup"]
            fail = False
            retried = await run_request(
                h,
                rt,
                await request(
                    h,
                    retry={
                        "type": "retry",
                        "source_run_id": asked.identity.run_id,
                        "task_ids": ["lookup"],
                    },
                ),
            )
            assert retried.response.status == "succeeded"
            assert calls == ["place_info", "parking", "parking"]
            assert h.model.interpret.await_count == 2
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_expiry_cleanup_preserves_shared_data_and_unrelated_trip(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            recommend(h)
            await run_request(h, rt, await request(h))
            # Another conversation stays live; cleanup must be ownership-scoped.
            other = await h.store.create_conversation(h.credential)
            async with h.writers() as db:
                places_before = await db.scalar(select(func.count()).select_from(Place))
            await h.store.delete_conversation(h.credential, h.conversation.conversation_id)
            with pytest.raises(StorageRejected, match="EXPIRED"):
                await h.current()
            assert await h.store.cleanup() == 1
            async with h.writers() as db:
                assert await db.get(ChatSession, h.conversation.trip_id) is None
                assert await db.get(ChatConversation, other.conversation_id) is not None
                assert await db.scalar(select(func.count()).select_from(Place)) == places_before
            now = h.store.clock()
            h.store.clock = lambda: now + timedelta(hours=25)
            assert await h.store.cleanup() == 1
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_expired_queue_and_unsupported_implementation_never_call_model(normalized_database):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            now = h.store.clock()
            h.store.clock = lambda: now + timedelta(seconds=11)
            await rt.tick()
            expired = await h.store.get_run(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            assert expired.response.error.code == "DEADLINE_EXCEEDED"
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            async with h.writers.begin() as db:
                await db.execute(
                    update(ChatRun)
                    .where(ChatRun.run_id == accepted.identity.run_id)
                    .values(implementation_version="future")
                )
            await rt.tick()
            unsupported = await h.store.get_run(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            assert unsupported.response.status == "interrupted" and not unsupported.retryable_tasks
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            async with h.writers.begin() as db:
                row = await db.get(ChatRun, accepted.identity.run_id)
                row.request = {**row.request, "schema_version": 999}
            await rt.tick()
            unsupported = await h.store.get_run(
                h.credential, h.conversation.conversation_id, accepted.identity.run_id
            )
            assert unsupported.response.status == "interrupted" and not unsupported.retryable_tasks
            assert h.model.interpret.await_count == 0
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())
