"""Bounded information tasks around one existing recommendation transaction."""

from dataclasses import dataclass, field
from uuid import uuid4

from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import InformationRequest, Interpretation
from app.schemas.pending import ChatTarget, ChatTaskResult
from app.schemas.recommendations import Clarification, Conditions, ConditionsPatch
from app.services.chat_conditions import current_draft, resolve_conditions
from app.services.pending import PendingFailure

TOPICS = {
    "recommendation_reason": "당시 추천 이유",
    "travel_time": "현재 자동차 이동시간",
    "parking": "현재 등록 주차 정보",
    "population": "현재 인구 현황",
    "scope_notice": "지원 범위",
}
PROVIDER_ERRORS = {
    "PROVIDER_TIMEOUT",
    "PROVIDER_ERROR",
    "SERVICE_UNAVAILABLE",
    "RATE_LIMITED",
    "DATA_UNAVAILABLE",
    "ROUTE_FAILED",
}


@dataclass
class PendingInformation:
    task_id: str
    topic: str
    target: ChatTarget | None
    target_kind: str = "selected"
    result_id: str | None = None
    state: str = "needs_input"
    paused: bool = False


@dataclass
class ConversationDraft:
    version: int
    conditions: Conditions
    tasks: list[PendingInformation] = field(default_factory=list)
    gate: str | None = None
    gate_quote: str | None = None
    gate_tasks: list[str] = field(default_factory=list)

    def context(self):
        return {
            "pending_information": [
                {
                    "topic": item.topic,
                    "status": "paused" if item.paused else item.state,
                    "target": item.target_kind,
                }
                for item in self.tasks
            ],
            "recommendation_waiting_for": self.gate,
            "condition_quote": self.gate_quote,
        }


def current_tasks(request, work):
    draft = work.conversation_draft
    if (
        isinstance(draft, ConversationDraft)
        and not work.expired
        and draft.version == request.condition_version
        and draft.conditions == request.current_conditions
    ):
        return draft
    return None


def needs_tasks(request, parsed, work):
    return bool(
        parsed.information_requests
        or parsed.recommendation_requested
        or parsed.request_relation != "independent"
        or parsed.resume_information
        or parsed.dialogue_scope != "conditions"
        or current_tasks(request, work)
        or (
            parsed.intent in TOPICS
            and parsed.intent != "scope_notice"
            and (
                parsed.patch().model_fields_set
                or parsed.origin_name
                or parsed.headcount_hint
                or parsed.clarification_fields
            )
        )
    )


class ChatTasksMixin:
    async def dispatch_tasks(self, request, parsed, stored, work, origin_draft, transition):
        draft = current_tasks(request, work) or ConversationDraft(
            request.condition_version, request.current_conditions.model_copy(deep=True)
        )
        work.conversation_draft = draft
        infos = list(parsed.information_requests)
        if not infos and parsed.intent in TOPICS and not parsed.resume_information:
            infos = [
                InformationRequest(
                    topic=parsed.intent,
                    target="unresolved" if parsed.target_ambiguous else "selected",
                    quote=request.message[:120],
                )
            ]
        scope_conditions = parsed.dialogue_scope in {"conditions", "all"}
        scope_information = parsed.dialogue_scope in {"information", "all"}
        control = parsed.dialogue_action in {"cancel", "pause", "restart"}
        if origin_draft is not None and control and scope_conditions:
            raise ServiceError(
                "AI_INTERPRETATION_FAILED",
                "출발지 확인 중에는 후보 선택 또는 새 출발지 입력으로 이어 주세요.",
                retryable=False,
            )
        if control and scope_information:
            if parsed.dialogue_action != "pause":
                draft.tasks.clear()
                if draft.gate == "information_first" and not scope_conditions:
                    draft.gate, draft.gate_quote = "conditional", "선행 정보 확인 취소"
            else:
                for item in draft.tasks:
                    item.paused = True
            infos = []
        if control and scope_conditions and parsed.dialogue_action in {"cancel", "restart"}:
            draft.gate = draft.gate_quote = None

        tasks = []
        remaining = list(draft.tasks)
        if parsed.resume_information and not (control and scope_information):
            tasks, remaining = remaining, []
            for item in tasks:
                item.paused = False
        for info in infos:
            match = next(
                (
                    item
                    for item in remaining
                    if item.topic == info.topic
                    and (item.target is None or item.target == request.target)
                ),
                None,
            )
            if match:
                remaining.remove(match)
                match.paused = False
                if (
                    info.target == "selected"
                    and request.target
                    and not parsed.target_ambiguous
                    and (match.result_id is None or match.result_id == request.target.result_id)
                    and (match.target_kind != "new_result" or match.result_id is not None)
                ):
                    match.target = request.target.model_copy(deep=True)
                tasks.append(match)
            elif not any(
                item.topic == info.topic
                and (parsed.resume_information or item.target_kind == info.target)
                for item in tasks
            ):
                selected = info.target == "selected" and not parsed.target_ambiguous
                tasks.append(
                    PendingInformation(
                        task_id=str(uuid4()),
                        topic=info.topic,
                        target=request.target.model_copy(deep=True)
                        if selected and request.target
                        else None,
                        target_kind=info.target
                        if selected
                        else ("new_result" if info.target == "new_result" else "unresolved"),
                        result_id=request.target.result_id
                        if request.target and info.target != "new_result"
                        else None,
                    )
                )
        if len(tasks) + len(remaining) > 5:
            raise ServiceError(
                "AI_INTERPRETATION_FAILED",
                "대기 중인 정보 질문이 많아요. 먼저 기존 질문을 마치거나 취소해 주세요.",
                retryable=False,
            )

        changes = bool(
            parsed.patch().model_fields_set
            or parsed.origin_name
            or parsed.origin_reply
            or parsed.headcount_hint
            # A read may carry an echoed old condition question. A condition
            # request (including the legacy intent) must justify asking it again.
            or (
                parsed.clarification_fields
                and (
                    parsed.recommendation_requested
                    or parsed.intent in {"recommendation", "clarification"}
                    or not infos
                )
            )
        )
        wants_conditions = (
            parsed.recommendation_requested
            or changes
            or (
                parsed.intent in {"recommendation", "clarification"}
                and not infos
                and not parsed.resume_information
            )
            or (control and scope_conditions)
        )
        if (
            parsed.intent == "scope_notice"
            and not parsed.recommendation_requested
            and not (control and scope_conditions)
        ):
            wants_conditions = False
        if control and not scope_conditions:
            wants_conditions = False
        if wants_conditions:
            await self.execution.check_current(request, work)
        if parsed.dialogue_action == "proceed" and scope_conditions:
            draft.gate = draft.gate_quote = None
        if parsed.request_relation == "conditional" and wants_conditions:
            draft.gate, draft.gate_quote = "conditional", parsed.relation_quote
        if parsed.request_relation == "information_first" and wants_conditions:
            draft.gate, draft.gate_quote = "information_first", parsed.relation_quote
            draft.gate_tasks = [item.task_id for item in tasks]

        condition_parsed = parsed.model_copy(
            update={
                "intent": "clarification" if parsed.intent == "clarification" else "recommendation",
                "target_ambiguous": False,
                "dialogue_action": parsed.dialogue_action if scope_conditions else "continue",
            }
        )
        results, verified = [], []
        primary, failure = None, None

        async def conditions():
            nonlocal primary, failure
            try:
                primary = await self.dispatch_single(
                    request,
                    condition_parsed,
                    condition_parsed.patch(),
                    stored,
                    work,
                    origin_draft,
                    transition,
                )
            except PendingFailure as exc:
                primary, failure = exc.data, exc

        async def information():
            for item in tasks:
                outcome = await self.run_information(
                    request,
                    parsed,
                    item,
                    stored,
                    verified,
                    new_result=primary.result if primary else None,
                )
                results.append(outcome)
                if outcome.status != "completed":
                    remaining.append(item)

        # Explicit sequencing only. Independent reads use the original card before mutation.
        if parsed.request_relation == "recommendation_first" and wants_conditions:
            await conditions()
            if primary.condition_update.applied and failure is None:
                await information()
            else:
                for item in tasks:
                    item.state = "waiting"
                    remaining.append(item)
                    results.append(
                        ChatTaskResult(
                            task_id=item.task_id,
                            topic=item.topic,
                            status="waiting",
                            message=f"조건 확인 뒤 {TOPICS[item.topic]}를 이어서 확인할게요.",
                        )
                    )
        else:
            await information()
            # An explicit continuation of blocked information may also unblock its change.
            waiting_for_information = any(
                item.state in {"needs_input", "failed", "waiting"}
                for item in remaining
                if item.task_id in draft.gate_tasks
            )
            if draft.gate == "information_first" and not waiting_for_information:
                draft.gate = draft.gate_quote = None
                wants_conditions = True
            if wants_conditions and not draft.gate:
                await conditions()
            elif wants_conditions and draft.gate:
                if origin_draft is not None or parsed.origin_name or parsed.origin_reply:
                    # Preserve origin tokens/candidates; do not bypass that confirmation flow.
                    condition_parsed = condition_parsed.model_copy(
                        update={
                            "intent": "clarification",
                            "question": "먼저 요청한 확인을 마쳐야 조건 변경을 진행할 수 있어요. "
                            "조건을 보장하지 않고 진행하려면 그렇게 말씀해 주세요.",
                        }
                    )
                    await conditions()
                else:
                    work.condition_draft, _ = resolve_conditions(
                        request, condition_parsed, current_draft(request, work)
                    )
                    message = (
                        f"‘{draft.gate_quote}’ 조건의 충족 여부는 자동으로 확정할 수 없어요. "
                        "이 조건을 보장하지 않고 변경을 진행할까요?"
                        if draft.gate == "conditional"
                        else "먼저 요청한 정보 확인이 끝나면 보관한 조건 변경을 이어갈게요."
                    )
                    primary = self.response(
                        request,
                        "clarification",
                        message,
                        clarification=Clarification(
                            question=message,
                            required_fields=["dependency"],
                            pending_patch=work.condition_draft.clarification.pending_patch,
                            choices=[],
                        ),
                    )
        # Deferred sequence resumes only after its change has actually been accepted.
        if primary is not None and primary.condition_update.applied and failure is None:
            deferred = [item for item in remaining if item.state == "waiting" and not item.paused]
            for item in deferred:
                remaining.remove(item)
                outcome = await self.run_information(
                    request, parsed, item, stored, verified, new_result=primary.result
                )
                results.append(outcome)
                if outcome.status != "completed":
                    remaining.append(item)
        for target, loaded in verified:
            self.contexts.verify_loaded(
                loaded,
                target.context_token,
                trip_id=request.trip_id,
                result_id=target.result_id,
                place_id=target.place_id,
            )
            if target not in work.answered_targets:
                work.answered_targets.append(target.model_copy(deep=True))
        draft.tasks = remaining
        if primary is None:
            completed = next((item.answer for item in results if item.answer), None)
            primary = (
                self.clarify_intent(request, parsed.question)
                if parsed.intent == "intent_clarification" and not results
                else self.response(request, "answer", completed.text, answer=completed)
                if completed
                else self.conversation_notice(
                    request,
                    "대기 중인 정보 요청을 정리했어요."
                    if control and scope_information
                    else "대기 중인 정보 요청이 없어요."
                    if not results
                    else results[0].message,
                )
            )
        parts = [item.message for item in results]
        if wants_conditions or not results:
            parts.append(primary.message)
        primary.message = "\n\n".join(dict.fromkeys(parts))
        primary.task_results = results
        if primary.result is None and any(
            item.topic == "travel_time" and item.status == "completed" for item in results
        ):
            primary.stages.routes = "ok"
        questions = [item.message for item in results if item.status == "needs_input"]
        if questions:
            primary.task_clarification = Clarification(
                question="\n".join(dict.fromkeys(questions)),
                required_fields=["target"],
                pending_patch=ConditionsPatch(),
                choices=[],
            )
        if primary.condition_update.applied:
            draft.version = primary.condition_update.version
            draft.conditions = primary.condition_update.conditions.model_copy(deep=True)
            # Bind new-result questions to this result, never to a later recommendation.
            for item in draft.tasks:
                if item.target_kind == "new_result" and primary.result:
                    item.result_id = str(primary.result["result_id"])
        if not draft.tasks and not draft.gate:
            work.conversation_draft = None
        if failure is not None:
            failure.data = primary
            raise failure
        return primary

    async def run_information(self, request, parsed, item, stored, verified, *, new_result=None):
        if item.topic == "scope_notice":
            data = await self.dispatch_single(
                request,
                Interpretation(intent="scope_notice"),
                ConditionsPatch(),
                stored,
                None,
                None,
                None,
            )
            return ChatTaskResult(
                task_id=item.task_id,
                topic=item.topic,
                status="completed",
                message=data.message,
                answer=data.answer,
            )
        if item.target is None and (parsed.resume_information or item.state == "failed"):
            selected = request.target
            if (
                selected
                and not parsed.target_ambiguous
                and (item.result_id is None or item.result_id == selected.result_id)
                and (item.target_kind != "new_result" or item.result_id is not None)
            ):
                item.target = selected.model_copy(deep=True)
        if item.target is None:
            item.state = "needs_input"
            if item.target_kind == "new_result" and new_result:
                item.result_id = str(new_result["result_id"])
            return ChatTaskResult(
                task_id=item.task_id,
                topic=item.topic,
                status="needs_input",
                message=f"{TOPICS[item.topic]}를 확인할 "
                + ("새 추천의 " if item.target_kind == "new_result" else "")
                + "대상 카드를 선택한 뒤 ‘이 카드로 이어서 알려줘’라고 말씀해 주세요.",
            )
        targeted = request.model_copy(update={"target": item.target}, deep=True)
        if item.target == request.target:
            loaded = stored
        else:
            loaded = await self.contexts.resolve(
                item.target.context_token,
                trip_id=request.trip_id,
                result_id=item.target.result_id,
                place_id=item.target.place_id,
            )
        self.verify_target(targeted, loaded)
        verified.append((item.target, loaded))
        try:
            data = await self.dispatch_single(
                targeted,
                Interpretation(intent=item.topic),
                ConditionsPatch(),
                loaded,
                None,
                None,
                None,
            )
        except ServiceError as exc:
            if exc.code not in PROVIDER_ERRORS:
                raise
            item.state = "failed"
            return ChatTaskResult(
                task_id=item.task_id,
                topic=item.topic,
                status="failed",
                message=f"{TOPICS[item.topic]} 확인에 실패했어요. {exc.message}",
                error_code=exc.code,
                retryable=exc.retryable is not False,
            )
        item.state = "completed"
        return ChatTaskResult(
            task_id=item.task_id,
            topic=item.topic,
            status="completed",
            message=data.answer.text,
            answer=data.answer,
        )
