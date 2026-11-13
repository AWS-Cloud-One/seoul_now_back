"""Writer-DB execution leases, cancellation, recovery and event ordering.

Every mutation locks conversation -> run. A receipt is never inferred from a
timeout. Recovery acquires the same locks as commit before declaring no write.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import object_session

from app.models import ChatConversation, ChatRun, ChatRunCommit, ChatRunEvent
from app.modules.chat_v2.continuations import continuation_response, saved_partial
from app.modules.chat_v2.contracts import ConversationContext, RunIdentity, RunRequest
from app.modules.chat_v2.responses import ChatResponse, DefaultResponseComposer
from app.modules.chat_v2.results import StageError
from app.modules.chat_v2.storage_contracts import (
    ExecutionClaim,
    RunEvent,
    StorageRejected,
    StoredContext,
)

IMPLEMENTATION = "p7-1-public"
LEASE_SECONDS = 30


def retry_tasks(run):
    if (
        run.phase != "finished"
        or run.response is None
        or run.implementation_version != IMPLEMENTATION
    ):
        return []
    response = ChatResponse.model_validate(run.response)
    op = run.operation or {"kind": "interpretation"}
    kind = op["kind"]
    if response.continuation and response.continuation.status in {"failed", "interrupted"}:
        return ["lookup"]
    if kind == "search" and response.lookup is not None:
        return ["search"] if response.lookup.outcome == "error" else []
    if kind == "lookup" and response.lookup is not None:
        return (
            ["lookup"]
            if any(i.status == "error" and i.retryable for i in response.lookup.items)
            else []
        )
    if response.snapshot and response.snapshot.finalization.commit.state == "committed":
        return (
            ["recommendation"] if response.snapshot.finalization.prepared.outcome == "error" else []
        )
    if response.error and response.error.retryable:
        return [kind]
    return []


class RunLifecycleStore:
    async def locate_run(self, credential, run_id):
        async with self.writers.begin() as db:
            browser = await self._authenticate(db, credential)
            conversation_id = await db.scalar(
                select(ChatRun.conversation_id)
                .join(
                    ChatConversation,
                    ChatRun.conversation_id == ChatConversation.conversation_id,
                )
                .where(ChatRun.run_id == run_id, ChatConversation.session_id == browser.session_id)
            )
            if conversation_id is None:
                raise StorageRejected("NOT_FOUND")
            return conversation_id

    def event(self, conversation, run, kind, **payload):
        # Reserve space for cancellation and final commit events; no heartbeat spam.
        seq = run.event_seq or 0
        if seq >= 195 and kind not in {
            "run_finished",
            "conditions_committed",
            "recommendation_committed",
            "cancellation_requested",
        }:
            return
        if seq >= 200:
            raise StorageRejected("EVENT_LIMIT")
        run.event_seq = seq + 1
        object_session(run).add(
            ChatRunEvent(
                run_id=run.run_id,
                event_seq=run.event_seq,
                type=kind,
                state_version=conversation.state_version,
                payload={"schema_version": 1, **payload},
                created_at=self.clock(),
            )
        )

    async def validate_retry(self, db, conversation, request):
        source = await db.scalar(
            select(ChatRun).where(
                ChatRun.run_id == request.input.source_run_id,
                ChatRun.conversation_id == conversation.conversation_id,
            )
        )
        if source is None:
            raise StorageRejected("NOT_FOUND")
        if (
            source.completed_state_version != conversation.state_version
            or request.expected_state_version != conversation.state_version
            or request.viewed_context is not None
        ):
            raise StorageRejected("VERSION_CONFLICT")
        if request.input.task_ids != retry_tasks(source):
            raise StorageRejected("NOT_RETRYABLE")
        return source

    async def events(self, credential, conversation_id, run_id, after_seq=0):
        if type(after_seq) is not int or not 0 <= after_seq <= 200:
            raise StorageRejected("INVALID_CURSOR")
        async with self.writers.begin() as db:
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            run = await db.scalar(
                select(ChatRun).where(
                    ChatRun.conversation_id == conversation_id,
                    ChatRun.run_id == run_id,
                )
            )
            if run is None:
                raise StorageRejected("NOT_FOUND")
            rows = (
                await db.scalars(
                    select(ChatRunEvent)
                    .where(
                        ChatRunEvent.run_id == run_id,
                        ChatRunEvent.event_seq > after_seq,
                    )
                    .order_by(ChatRunEvent.event_seq)
                )
            ).all()
            return await self.view(db, run, conversation), [
                RunEvent(
                    event_seq=r.event_seq,
                    type=r.type,
                    state_version=r.state_version,
                    payload=r.payload,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def checkpoint(self, claim, stage, operation=None):
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            if run.response is not None and run.phase != "post_commit":
                raise StorageRejected("OWNER_CONFLICT")
            await self.check_current(db, conversation, run, claim)
            if run.stage != stage:
                run.stage = stage
                self.event(conversation, run, "stage_started", stage=stage)
            if operation is not None:
                run.operation = operation

    async def renew(self, claim):
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            if run.response is not None and run.phase != "post_commit":
                return False
            await self.check_current(db, conversation, run, claim)
            run.lease_expires_at = min(
                run.deadline_at, self.clock() + timedelta(seconds=LEASE_SECONDS)
            )
            return True

    async def stop_locked(self, db, conversation, run, code):
        if run.phase == "post_commit":
            response = continuation_response(
                ChatResponse.model_validate(run.response),
                "cancelled" if code == "CANCELLED" else "interrupted",
                lookup=saved_partial(run.operation),
                reason=code,
            )
            self.end_continuation_locked(conversation, run, response)
            run.owner = uuid4()
            return await self.view(db, run, conversation)
        if run.response is not None:
            return await self.view(db, run, conversation)
        # Holding the same locks means any previous commit has ended. Receipt and
        # response are atomic; an inconsistent record is never converted to failure.
        response = DefaultResponseComposer().failed(
            ConversationContext.model_validate(run.context),
            StageError(
                code=code,
                safe_message={
                    "CANCELLED": "실행을 중단했어요. 이번 실행의 변경은 반영하지 않았어요.",
                    "INTERRUPTED": "실행이 중단됐어요. 반영되지 않은 작업은 다시 시도할 수 있어요.",
                    "DEADLINE_EXCEEDED": "실행 시간이 초과됐어요. 이번 변경은 반영하지 않았어요.",
                    "INTERNAL_ERROR": "실행을 완료하지 못했어요. 이번 변경은 반영하지 않았어요.",
                }[code],
                retryable=code != "CANCELLED" and run.implementation_version == IMPLEMENTATION,
            ),
        )
        if code == "INTERRUPTED" and run.implementation_version != IMPLEMENTATION:
            response.error.safe_message = (
                "저장된 실행 형식을 현재 버전에서 처리할 수 없어요. 요청을 새로 보내주세요."
            )
            response.answer = response.error.safe_message
        self.finish_locked(conversation, run, response)
        run.owner = uuid4()  # fence any late model/provider result from the old worker
        return await self.view(db, run, conversation)

    async def _no_orphan_receipt(self, db, run):
        if run.response is None and await db.get(ChatRunCommit, run.run_id) is not None:
            raise StorageRejected("COMMIT_UNCERTAIN")

    async def cancel(self, credential, conversation_id, run_id):
        async with self.writers.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            run = await db.scalar(
                select(ChatRun)
                .where(
                    ChatRun.run_id == run_id,
                    ChatRun.conversation_id == conversation_id,
                )
                .with_for_update()
            )
            if run is None:
                raise StorageRejected("NOT_FOUND")
            if run.phase == "finished":
                return await self.view(db, run, conversation)
            await self._no_orphan_receipt(db, run)
            run.cancel_requested_at = self.clock()
            self.event(conversation, run, "cancellation_requested")
            return await self.stop_locked(db, conversation, run, "CANCELLED")

    async def stop_claim(self, claim, code="INTERRUPTED"):
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            await self._no_orphan_receipt(db, run)
            return await self.stop_locked(db, conversation, run, code)

    async def compatible(self, db, conversation, run):
        """Unsupported stored inputs are interrupted, never interpreted by new code."""
        from app.repositories.recommendation_conditions import RecommendationConditionsRepository

        try:
            if run.implementation_version != IMPLEMENTATION:
                raise ValueError("Unsupported implementation")
            ConversationContext.model_validate(run.context)
            RunRequest.model_validate(run.request)
            if run.operation and (
                run.operation.get("schema_version") != 1
                or run.operation.get("kind")
                not in {"interpretation", "search", "lookup", "recommendation", "action"}
            ):
                raise ValueError("Unsupported operation")
            return True
        except ValueError:
            await self._no_orphan_receipt(db, run)
            current = await RecommendationConditionsRepository(db).get_current(conversation.trip_id)
            # A safe terminal projection from relational identity/current state only.
            # The unknown request is retained for diagnosis, never run or retried.
            run.context = ConversationContext(
                identity=RunIdentity(
                    conversation_id=run.conversation_id,
                    trip_id=run.trip_id,
                    run_id=run.run_id,
                    request_id=run.request_id,
                ),
                condition_version=current.version,
                state_version=conversation.state_version,
                current_conditions=current.conditions,
            ).model_dump(mode="json")
            run.implementation_version = "unsupported"
            await self.stop_locked(db, conversation, run, "INTERRUPTED")
            return False

    async def claim_next(self):
        """DB is the queue. Claim under the conversation lock; never steal running work."""
        async with self.writers.begin() as db:
            conversation = await db.scalar(
                select(ChatConversation)
                .join(
                    ChatRun,
                    ChatConversation.active_run_id == ChatRun.run_id,
                )
                .where(ChatRun.phase == "accepted")
                .order_by(ChatRun.created_at)
                .with_for_update(of=ChatConversation, skip_locked=True)
                .limit(1)
            )
            if conversation is None:
                return None
            run = await db.get(ChatRun, conversation.active_run_id, with_for_update=True)
            now = self.clock()
            if not await self.compatible(db, conversation, run):
                return None
            if min(run.created_at + timedelta(seconds=10), run.deadline_at) <= now:
                await self.stop_locked(db, conversation, run, "DEADLINE_EXCEEDED")
                return None
            run.owner, run.phase, run.stage = uuid4(), "running", "interpreting"
            if run.request["input"]["type"] == "action":
                run.stage = "applying_action"
            run.started_at = now
            run.deadline_at = min(run.deadline_at, now + timedelta(seconds=90))
            run.lease_expires_at = min(run.deadline_at, now + timedelta(seconds=LEASE_SECONDS))
            self.event(conversation, run, "stage_started", stage=run.stage)
            context = ConversationContext.model_validate(run.context)
            request = RunRequest.model_validate(run.request)
            claim = ExecutionClaim(
                context=context,
                owner=run.owner,
                excluded=tuple(StoredContext.model_validate(conversation.context).excluded),
                deadline_at=run.deadline_at,
                approved_action=run.operation if request.input.type == "action" else None,
            )
            source = None
            if request.input.type == "retry":
                source = await db.get(ChatRun, request.input.source_run_id)
                # Private, server-validated snapshot. Never supplied by the client.
                source = {
                    "request": source.request,
                    "operation": source.operation,
                    "response": source.response,
                }
            return claim, request, source

    async def recover(self, limit=20):
        """Finish expired executions without replaying their model or external reads."""
        count = 0
        async with self.writers.begin() as db:
            now = self.clock()
            rows = (
                await db.scalars(
                    select(ChatConversation)
                    .join(
                        ChatRun,
                        ChatConversation.active_run_id == ChatRun.run_id,
                    )
                    .where(
                        ChatRun.phase.in_(["running", "preparing_commit", "post_commit"]),
                        (ChatRun.lease_expires_at.is_(None))
                        | (ChatRun.lease_expires_at <= now)
                        | (ChatRun.deadline_at <= now),
                    )
                    .with_for_update(of=ChatConversation, skip_locked=True)
                    .limit(limit)
                )
            ).all()
            for conversation in rows:
                run = await db.get(ChatRun, conversation.active_run_id, with_for_update=True)
                await self._no_orphan_receipt(db, run)
                if not await self.compatible(db, conversation, run):
                    count += 1
                    continue
                await self.stop_locked(
                    db,
                    conversation,
                    run,
                    "DEADLINE_EXCEEDED" if run.deadline_at <= now else "INTERRUPTED",
                )
                count += 1
        return count

    async def delete_conversation(self, credential, conversation_id):
        async with self.writers.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            if conversation.active_run_id is not None:
                run = await db.get(ChatRun, conversation.active_run_id, with_for_update=True)
                await self._no_orphan_receipt(db, run)
                run.cancel_requested_at = self.clock()
                self.event(conversation, run, "cancellation_requested")
                await self.stop_locked(db, conversation, run, "CANCELLED")
            conversation.expires_at = max(
                self.clock(), conversation.created_at + timedelta(microseconds=1)
            )

    async def cleanup(self, limit=20):
        """Remove expired V2-owned data only; retain shared place/observation originals."""
        from sqlalchemy import delete

        from app.models import Base, ChatBrowserSession

        async with self.writers.begin() as db:
            rows = (
                await db.scalars(
                    select(ChatConversation)
                    .where(
                        ChatConversation.expires_at <= self.clock(),
                        ChatConversation.active_run_id.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
            for row in rows:
                run_ids = select(ChatRun.run_id).where(
                    ChatRun.conversation_id == row.conversation_id
                )
                actions = Base.metadata.tables["chat_actions"]
                await db.execute(
                    delete(actions).where(actions.c.conversation_id == row.conversation_id)
                )
                for name in ("chat_run_events", "chat_run_commits", "chat_results"):
                    table = Base.metadata.tables[name]
                    await db.execute(delete(table).where(table.c.run_id.in_(run_ids)))
                await db.execute(
                    delete(ChatRun).where(ChatRun.conversation_id == row.conversation_id)
                )
                await db.delete(row)
                await db.flush()
                results = Base.metadata.tables["recommendation_results"]
                result_ids = select(results.c.result_id).where(results.c.trip_id == row.trip_id)
                for name in (
                    "recommendation_evidence",
                    "recommendation_search_areas",
                    "recommendation_cards",
                ):
                    table = Base.metadata.tables[name]
                    await db.execute(delete(table).where(table.c.result_id.in_(result_ids)))
                for name in (
                    "recommendation_results",
                    "chat_messages",
                    "chat_excluded_places",
                    "chat_condition_age_groups",
                    "chat_condition_purposes",
                    "chat_condition_versions",
                    "chat_sessions",
                ):
                    table = Base.metadata.tables[name]
                    await db.execute(delete(table).where(table.c.trip_id == row.trip_id))
            # Lock expired parents before deleting, so parallel cleanup cannot race.
            sessions = (
                await db.scalars(
                    select(ChatBrowserSession)
                    .where(
                        ChatBrowserSession.expires_at <= self.clock(),
                        ~select(ChatConversation.conversation_id)
                        .where(
                            ChatConversation.session_id == ChatBrowserSession.session_id,
                        )
                        .exists(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
            for session in sessions:
                await db.delete(session)
            return len(rows)
