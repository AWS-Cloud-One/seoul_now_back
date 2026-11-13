"""Post-commit reads preserve their primary recommendation and receipt."""

from app.modules.chat_v2.dialogue_context import ContinuationResult, LookupResult
from app.modules.chat_v2.responses import ChatResponse, DefaultResponseComposer


def continuation_response(primary, status, *, lookup=None, reason=None, clarification=None):
    response = DefaultResponseComposer().compose(primary.snapshot.finalization)
    response.continuation = ContinuationResult(status=status, lookup=lookup, reason_code=reason)
    response.status = {
        "pending": "partial",
        "failed": "partial",
        "blocked": "partial",
        "waiting_input": "waiting_input",
        "cancelled": "cancelled",
        "interrupted": "interrupted",
        "completed": response.snapshot.status,
    }[status]
    response.clarification = clarification
    if status == "completed" or (status == "failed" and lookup):
        from app.modules.chat_v2.tools import explain_lookup

        response.answer += " " + explain_lookup(lookup)
    else:
        response.answer += (
            " "
            + {
                "pending": "새 추천 장소의 후속 정보를 조회하고 있어요.",
                "blocked": "새로 확정한 해당 장소가 없어 후속 조회를 하지 않았어요.",
                "failed": "추천은 유지했지만 후속 조회를 완료하지 못했어요.",
                "cancelled": "추천은 이미 저장됐고 남은 조회를 중단했어요.",
                "interrupted": "추천은 유지했고 중단된 후속 조회만 다시 시도할 수 있어요.",
                "waiting_input": clarification.question
                if clarification
                else "후속 조회에 필요한 입력을 알려주세요.",
            }[status]
        )
    return ChatResponse.model_validate(response.model_dump())


def saved_partial(operation):
    items = (operation or {}).get("items", [])
    return LookupResult(outcome="partial", items=items) if items else None


class ContinuationStore:
    def end_continuation_locked(self, conversation, run, response, *, emit_terminal=True):
        response = ChatResponse.model_validate(response.model_dump())
        if response.snapshot != ChatResponse.model_validate(run.response).snapshot:
            raise ValueError("A continuation cannot replace its committed primary result")
        conversation.state_version += 1
        conversation.active_run_id = None
        run.response = response.model_dump(mode="json")
        run.phase, run.stage = "finished", "finished"
        run.completed_state_version = conversation.state_version
        self.event(
            conversation, run, "task_completed", task="lookup", status=response.continuation.status
        )
        if emit_terminal:
            self.event(conversation, run, "run_finished", status=response.status)

    async def continuation_state(self, claim):
        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            if run.phase == "finished":
                return await self.view(db, run, conversation), run.operation
            await self.check_current(db, conversation, run, claim)
            return await self.view(db, run, conversation), run.operation

    async def finish_continuation(self, claim, *, lookup=None, pending_read=None):
        from app.modules.chat_v2.contracts import NeedsInputIntent
        from app.modules.chat_v2.storage_contracts import StorageRejected, StoredContext

        async with self.writers.begin() as db:
            conversation, run = await self.locked_claim(db, claim)
            if run.phase == "finished":
                return await self.view(db, run, conversation)
            if run.phase != "post_commit":
                raise StorageRejected("OWNER_CONFLICT")
            await self.check_current(db, conversation, run, claim)
            clarification = None
            if pending_read:
                state = StoredContext.model_validate(conversation.context)
                state.pending_read = pending_read
                conversation.context = state.model_dump(mode="json")
                clarification = NeedsInputIntent(
                    kind="needs_input",
                    required_field="lookup_origin",
                    question=pending_read.question,
                )
            status = (
                "waiting_input"
                if pending_read
                else "failed"
                if lookup.outcome in {"error", "partial"}
                else "completed"
            )
            response = continuation_response(
                ChatResponse.model_validate(run.response),
                status,
                lookup=lookup,
                clarification=clarification,
            )
            self.end_continuation_locked(conversation, run, response, emit_terminal=False)
            await self.publish_question(db, conversation, run, response)
            self.event(conversation, run, "run_finished", status=response.status)
            return await self.view(db, run, conversation)
