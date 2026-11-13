"""Writer transactions for owned conversations, admission and atomic publication.

The lifecycle mixin adds leased background execution, cancellation and recovery.
Direct internal executions retain the same storage and finalization boundary.
"""

import hashlib
import hmac
import json
import re
import secrets
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text

from app.models import ChatBrowserSession, ChatConversation, ChatRun, ChatSession
from app.modules.chat_v2.continuations import ContinuationStore
from app.modules.chat_v2.contracts import (
    ConversationContext,
    PendingHeadcountQuestion,
    RunIdentity,
    RunRequest,
    ViewedContext,
)
from app.modules.chat_v2.dialogue_context import RecommendationFlow
from app.modules.chat_v2.history import HistoryStore
from app.modules.chat_v2.interaction_store import InteractionStore
from app.modules.chat_v2.lifecycle_store import IMPLEMENTATION, RunLifecycleStore, retry_tasks
from app.modules.chat_v2.presentation_contracts import UserMessage
from app.modules.chat_v2.references import (
    merge_references,
    owned_recommendation,
    recommendation_references,
)
from app.modules.chat_v2.responses import ChatResponse
from app.modules.chat_v2.storage_contracts import (
    ConversationSnapshot,
    ExecutionClaim,
    ExecutionView,
    SessionCredential,
    StorageRejected,
    StoredContext,
)
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.schemas.recommendations import Conditions


def utcnow():
    return datetime.now(UTC)


def request_digest(request):
    value = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


class ChatV2Store(RunLifecycleStore, ContinuationStore, HistoryStore, InteractionStore):
    def __init__(self, writers, *, clock=utcnow, budgets=None):
        # Must target the writer, including reconciliation reads (never a replica).
        self.writers, self.clock = writers, clock
        self.budgets = budgets

    async def check_ready(self):
        """Fail startup before serving if the required V2 schema/read grants are absent."""
        async with self.writers() as db:
            await db.execute(
                text("SELECT sequence, question, implementation_version FROM chat_runs LIMIT 0")
            )
            await db.execute(text("SELECT action_id FROM chat_actions LIMIT 0"))
            await db.execute(text("SELECT session_id FROM chat_browser_sessions LIMIT 0"))
            await db.execute(text("SELECT used FROM chat_usage_buckets LIMIT 0"))

    async def create_session(self, *, peer="internal"):
        now, token = self.clock(), secrets.token_urlsafe(32)
        credential = SessionCredential(
            session_id=uuid4(), token=token, expires_at=now + timedelta(hours=24)
        )
        async with self.writers.begin() as db:
            if self.budgets:
                await self.budgets.session(db, peer)
            db.add(
                ChatBrowserSession(
                    session_id=credential.session_id,
                    token_digest=hashlib.sha256(token.encode()).hexdigest(),
                    created_at=now,
                    expires_at=credential.expires_at,
                )
            )
        return credential

    async def _authenticate(self, db, credential):
        token = credential.token.get_secret_value()
        row = await db.get(ChatBrowserSession, credential.session_id)
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            or row is None
            or not hmac.compare_digest(row.token_digest, hashlib.sha256(token.encode()).hexdigest())
        ):
            raise StorageRejected("UNAUTHORIZED")
        # Supplied expires_at is informational; only the database decides validity.
        if row.expires_at <= self.clock():
            raise StorageRejected("EXPIRED")
        return row

    async def _owned(self, db, credential, conversation_id, *, lock=False):
        browser = await self._authenticate(db, credential)
        query = select(ChatConversation).where(
            ChatConversation.conversation_id == conversation_id,
            ChatConversation.session_id == browser.session_id,
        )
        if lock:
            query = query.with_for_update()
        conversation = await db.scalar(query)
        if conversation is None:
            raise StorageRejected("NOT_FOUND")
        if conversation.expires_at <= self.clock():
            raise StorageRejected("EXPIRED")
        return conversation

    async def create_conversation(
        self, credential, *, initial_conditions: Conditions | None = None
    ):
        """Internal bootstrap with SERVER-VERIFIED conditions; not a client POST body.

        A new trip is always allocated. P2 supplies origin/purpose confirmation;
        this method cannot attach a caller-selected V1 trip or arbitrary question.
        """
        conditions = (
            Conditions.model_validate(initial_conditions.model_dump())
            if initial_conditions is not None
            else Conditions(
                origin=None,
                purposes=[],
                headcount=None,
                group_type=None,
                age_groups=None,
                max_travel_seconds=None,
            )
        )
        now, trip_id, conversation_id = self.clock(), uuid4(), uuid4()
        async with self.writers.begin() as db:
            browser = await self._authenticate(db, credential)
            if self.budgets:
                await self.budgets.conversation(db, browser.session_id)
            expires = min(browser.expires_at, now + timedelta(hours=24))
            await RecommendationConditionsRepository(db).accept(
                trip_id, 0, conditions, conditions, expected_existing=False
            )
            trip = await db.get(ChatSession, trip_id)
            trip.expires_at = expires
            conversation = ChatConversation(
                conversation_id=conversation_id,
                session_id=browser.session_id,
                trip_id=trip_id,
                state_version=0,
                context=StoredContext().model_dump(mode="json"),
                created_at=now,
                expires_at=expires,
            )
            db.add(conversation)
        return await self.conversation(credential, conversation_id)

    async def conversation(self, credential, conversation_id):
        async with self.writers.begin() as db:
            row = await self._owned(db, credential, conversation_id, lock=True)
            current = await RecommendationConditionsRepository(db).get_current(row.trip_id)
            if current is None:
                raise StorageRejected("NOT_FOUND")
            return ConversationSnapshot(
                conversation_id=row.conversation_id,
                trip_id=row.trip_id,
                condition_version=current.version,
                state_version=row.state_version,
                conditions=current.conditions,
                pending_question=StoredContext.model_validate(row.context).pending_question,
                active_run_id=row.active_run_id,
                expires_at=row.expires_at,
                questions=await self.current_questions(db, row),
                **StoredContext.model_validate(row.context).model_dump(
                    exclude={"schema_version", "excluded", "pending_question"}
                ),
            )

    async def view(self, db, run, conversation):
        response = ChatResponse.model_validate(run.response) if run.response else None
        return ExecutionView(
            identity=ConversationContext.model_validate(run.context).identity,
            state_version=run.completed_state_version if response else conversation.state_version,
            status="finished"
            if run.phase == "finished"
            else "running"
            if run.phase == "post_commit"
            else "reconciling"
            if run.phase == "preparing_commit"
            else run.phase,
            response=response,
            stage=run.stage,
            event_seq=run.event_seq,
            cancel_requested_at=run.cancel_requested_at,
            retryable_tasks=retry_tasks(run),
            question=await self.question_view(db, conversation, run),
        )

    async def admit(
        self,
        credential,
        conversation_id,
        request: RunRequest,
        *,
        dialogue=False,
        queued=False,
        peer="internal",
    ):
        request = RunRequest.model_validate(request.model_dump())
        async with self.writers.begin() as db:
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            prior = await db.scalar(
                select(ChatRun).where(
                    ChatRun.conversation_id == conversation_id,
                    (ChatRun.request_id == request.request_id)
                    | ChatRun.request_aliases.has_key(str(request.request_id)),
                )
            )
            if prior is not None:
                digest = (
                    prior.request_hash
                    if prior.request_id == request.request_id
                    else prior.request_aliases[str(request.request_id)]
                )
                if digest != request_digest(request):
                    raise StorageRejected("REQUEST_ID_CONFLICT")
                return await self.view(db, prior, conversation)
            action = None
            if request.input.type == "action":
                if not dialogue:
                    raise StorageRejected("UNSUPPORTED_ACTION")
                action = await self.owned_action(db, conversation, request.input.action_id)
                if action.consumed_by_run_id:
                    consumed = await db.get(ChatRun, action.consumed_by_run_id)
                    consumed.request_aliases = {
                        **consumed.request_aliases,
                        str(request.request_id): request_digest(request),
                    }
                    return await self.view(db, consumed, conversation)
            if conversation.active_run_id is not None:
                raise StorageRejected("RUN_ACTIVE")
            current = await RecommendationConditionsRepository(db).get_current(conversation.trip_id)
            if current is None or (
                not dialogue
                and (
                    current.version != request.expected_condition_version
                    or conversation.state_version != request.expected_state_version
                )
            ):
                raise StorageRejected("VERSION_CONFLICT")
            retry_source = None
            approved_action = (
                self.validate_action(action, conversation, current, request) if action else None
            )
            if request.input.type == "retry":
                if not queued or current.version != request.expected_condition_version:
                    raise StorageRejected("VERSION_CONFLICT")
                retry_source = await self.validate_retry(db, conversation, request)
            # No context-reference reader in F02 yet: never silently ignore a target.
            if request.viewed_context is not None and not dialogue:
                raise StorageRejected("UNSUPPORTED_REFERENCE")
            stored = StoredContext.model_validate(conversation.context)
            references = list(stored.references)
            active_flow = stored.active_flow
            viewed = request.viewed_context
            if retry_source is not None:
                value = (retry_source.operation or {}).get(
                    "viewed_context"
                ) or retry_source.request.get("viewed_context")
                viewed = ViewedContext.model_validate(value) if value else None
            if viewed:
                if (
                    viewed.result_id
                    and viewed.collection_id
                    and viewed.result_id != viewed.collection_id
                ):
                    raise StorageRejected("NOT_FOUND")
                if viewed.result_id:
                    result = await owned_recommendation(db, conversation_id, viewed.result_id)
                    if result is None:
                        raise StorageRejected("NOT_FOUND")
                    references = merge_references(
                        references, recommendation_references(result), limit=35
                    )
                    active_flow = self.result_flow(result)
                    if active_flow:
                        # Viewing an earlier card must not rewind this flow's seen set.
                        latest = await db.scalar(
                            select(ChatRun.response)
                            .where(
                                ChatRun.conversation_id == conversation_id,
                                ChatRun.response["recommendation"]["flow_id"].as_string()
                                == str(active_flow.flow_id),
                            )
                            .order_by(ChatRun.created_at.desc())
                            .limit(1)
                        )
                        if latest:
                            latest_flow = self.result_flow(ChatResponse.model_validate(latest))
                            active_flow.excluded = sorted(
                                set(active_flow.excluded) | set(latest_flow.excluded)
                            )
                group = viewed.result_id or viewed.collection_id
                visible = [r for r in references if group is None or r.collection_id == group]
                if (group is not None and not visible) or (
                    viewed.place_ref and not any(r.ref_id == viewed.place_ref for r in visible)
                ):
                    raise StorageRejected("NOT_FOUND")
            reply = getattr(request.input, "reply_to_clarification_id", None)
            question_ids = {
                q.clarification_id
                for q in (stored.pending_question, stored.pending_read)
                if q is not None
            }
            if any(
                ref is not None and ref not in question_ids
                for ref in (reply, viewed.clarification_id if viewed else None)
            ):
                raise StorageRejected("VERSION_CONFLICT")
            if self.budgets:
                await self.budgets.run(db, credential.session_id, peer)
            now, owner, run_id = self.clock(), uuid4(), uuid4()
            context = ConversationContext(
                identity=RunIdentity(
                    conversation_id=conversation_id,
                    trip_id=conversation.trip_id,
                    run_id=run_id,
                    request_id=request.request_id,
                ),
                condition_version=current.version,
                state_version=conversation.state_version,
                current_conditions=current.conditions,
                pending_question=stored.pending_question,
                search_area=stored.search_area,
                draft=stored.draft,
                references=references,
                focus_ref=stored.focus_ref,
                pending_read=stored.pending_read,
                active_flow=active_flow,
            )
            deadline = min(conversation.expires_at, now + timedelta(seconds=120))
            operation = {
                "schema_version": 1,
                "kind": "interpretation",
                "input": request.input.model_dump(mode="json"),
                "viewed_context": viewed.model_dump(mode="json") if viewed else None,
            }
            if retry_source is not None:
                operation = deepcopy(
                    retry_source.operation
                    or {
                        "schema_version": 1,
                        "kind": "interpretation",
                        "input": retry_source.request["input"],
                        "viewed_context": retry_source.request.get("viewed_context"),
                    }
                )
            if approved_action:
                operation = approved_action
            sequence = (
                await db.scalar(
                    select(func.coalesce(func.max(ChatRun.sequence), 0)).where(
                        ChatRun.conversation_id == conversation_id,
                    )
                )
                + 1
            )
            display = UserMessage(
                message_id=f"{run_id}:user",
                input_type=request.input.type,
                text=request.input.text
                if request.input.type == "message"
                else action.label
                if action
                else "실패 작업 재시도",
                source_run_id=request.input.source_run_id
                if request.input.type == "retry"
                else None,
            )
            run = ChatRun(
                run_id=run_id,
                conversation_id=conversation_id,
                trip_id=conversation.trip_id,
                request_id=request.request_id,
                sequence=sequence,
                user_message=display.model_dump(mode="json"),
                request_hash=request_digest(request),
                owner=owner,
                request=request.model_dump(mode="json"),
                context=context.model_dump(mode="json"),
                operation=operation,
                phase="accepted" if queued else "running",
                stage="accepted" if queued else "interpreting",
                implementation_version=IMPLEMENTATION,
                started_at=None if queued else now,
                lease_expires_at=None if queued else deadline,
                event_seq=0,
                created_at=now,
                deadline_at=deadline,
            )
            db.add(run)
            conversation.active_run_id = run_id
            # Flush the parent conversation and child run in explicit order.
            await db.flush()
            if action:
                action.consumed_by_run_id = run_id
            self.event(conversation, run, "run_accepted")
            if queued:
                view = await self.view(db, run, conversation)
        if queued:
            return view
        return ExecutionClaim(
            context=context,
            owner=owner,
            excluded=tuple(stored.excluded),
            deadline_at=deadline,
            approved_action=approved_action,
        )

    async def locked_claim(self, db, claim):
        # Same lock order in finalization, finish and reconciliation. A timeout is
        # uncertainty, never proof of rollback or permission to repeat the write.
        await db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        identity = claim.context.identity
        conversation = await db.scalar(
            select(ChatConversation)
            .where(ChatConversation.conversation_id == identity.conversation_id)
            .with_for_update()
        )
        run = await db.scalar(
            select(ChatRun).where(ChatRun.run_id == identity.run_id).with_for_update()
        )
        if (
            conversation is None
            or run is None
            or run.owner != claim.owner
            or ConversationContext.model_validate(run.context) != claim.context
            or conversation.trip_id != identity.trip_id
        ):
            raise StorageRejected("OWNER_CONFLICT")
        if run.phase != "finished" and conversation.active_run_id != identity.run_id:
            raise StorageRejected("OWNER_CONFLICT")
        return conversation, run

    async def check_current(self, db, conversation, run, claim):
        browser = await db.get(ChatBrowserSession, conversation.session_id)
        if min(conversation.expires_at, browser.expires_at, run.deadline_at) <= self.clock():
            raise StorageRejected("DEADLINE_EXCEEDED")
        if run.cancel_requested_at is not None or (
            run.lease_expires_at is not None and run.lease_expires_at <= self.clock()
        ):
            raise StorageRejected("OWNER_CONFLICT")
        current = await RecommendationConditionsRepository(db).get_current(conversation.trip_id)
        committed = (
            ChatResponse.model_validate(run.response) if run.phase == "post_commit" else None
        )
        if (
            current is None
            or current.version
            != (committed.condition_version if committed else claim.context.condition_version)
            or current.conditions
            != (committed.conditions if committed else claim.context.current_conditions)
            or conversation.state_version
            != (run.completed_state_version if committed else claim.context.state_version)
        ):
            raise StorageRejected("VERSION_CONFLICT")

    def finish_locked(self, conversation, run, response, *, keep_active=False, emit_terminal=True):
        response = ChatResponse.model_validate(response.model_dump())
        if response.identity != ConversationContext.model_validate(run.context).identity:
            raise StorageRejected("OWNER_CONFLICT")
        if response.status == "reconciling":
            raise ValueError("Uncertain execution cannot be finished")
        context = StoredContext.model_validate(conversation.context)
        if response.snapshot and response.snapshot.finalization.commit.state == "committed":
            context.pending_question = None
            context.draft = None
            plan = response.snapshot.finalization.prepared.plan
            if plan.feature_id == "F01" and plan.scope == "persistent":
                context.search_area = plan.search_area
                context.active_flow = None
            if response.recommendation:
                refs = recommendation_references(response)
                context.references = merge_references(context.references, refs)
                context.focus_ref = refs[0].ref_id if len(refs) == 1 else None
                context.excluded = sorted(
                    set(context.excluded)
                    | {c.place.value.place_id for c in response.recommendation.cards}
                )
                if plan.scope == "request_only":
                    context.active_flow = self.result_flow(response)
        elif response.kind == "unchanged":
            context.pending_question = None
        elif response.clarification and response.clarification.required_field not in {
            "target",
            "lookup_origin",
        }:
            context.pending_question = PendingHeadcountQuestion(
                clarification_id=str(uuid4()),
                scope="persistent",
                question=response.clarification.question,
                field=response.clarification.required_field,
            )
        conversation.context = context.model_dump(mode="json")
        conversation.state_version += 1
        conversation.active_run_id = run.run_id if keep_active else None
        run.response = response.model_dump(mode="json")
        run.phase = "post_commit" if keep_active else "finished"
        run.stage = "looking_up" if keep_active else "finished"
        run.completed_state_version = conversation.state_version
        if response.snapshot and response.snapshot.finalization.commit.state == "committed":
            receipt = response.snapshot.finalization.commit.receipt
            if receipt.changed_fields:
                self.event(
                    conversation,
                    run,
                    "conditions_committed",
                    condition_version=receipt.current_version,
                )
            if receipt.result_id:
                self.event(
                    conversation, run, "recommendation_committed", result_id=str(receipt.result_id)
                )
        if keep_active:
            self.event(conversation, run, "stage_started", stage="looking_up")
        elif emit_terminal:
            self.event(conversation, run, "run_finished", status=response.status)

    @staticmethod
    def result_flow(response):
        if response.recommendation is None or response.recommendation.scope != "request_only":
            return None
        plan = response.snapshot.finalization.prepared.plan
        return RecommendationFlow(
            flow_id=plan.flow_id,
            result_id=response.recommendation.result_id,
            conditions=plan.effective_conditions,
            search_area=plan.search_area,
            constraints=plan.constraints,
            excluded=sorted(
                set(plan.flow_excluded)
                | {c.place.value.place_id for c in response.recommendation.cards}
            ),
        )

    async def finish_early(self, claim, response, *, state_update=None):
        if response.snapshot is not None:
            raise ValueError("Finalization is owned by the atomic finalizer")
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            if run.response is None:
                await self.check_current(db, conversation, run, claim)
                self.finish_locked(conversation, run, response, emit_terminal=False)
                if state_update is not None:
                    conversation.context = StoredContext.model_validate(
                        state_update.model_dump()
                    ).model_dump(mode="json")
                await self.publish_question(db, conversation, run, response)
                self.event(conversation, run, "run_finished", status=response.status)
        return await self.read_claim(claim)

    async def read_claim(self, claim):
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            return await self.view(db, run, conversation)

    async def get_run(self, credential, conversation_id, run_id):
        async with self.writers.begin() as db:
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            run = await db.scalar(
                select(ChatRun).where(
                    ChatRun.run_id == run_id, ChatRun.conversation_id == conversation_id
                )
            )
            if run is None:
                raise StorageRejected("NOT_FOUND")
            return await self.view(db, run, conversation)
