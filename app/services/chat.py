"""C01 adapter: interpret once, then call the same structured recommendation service."""

from dataclasses import replace

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation, interpret
from app.schemas.pending import Answer, ChatData, ChatStages
from app.schemas.recommendations import (
    Clarification,
    Conditions,
    ConditionsPatch,
    ConditionUpdate,
    RecommendationRequest,
)
from app.services.chat_conditions import current_draft, is_condition_question, resolve_conditions
from app.services.chat_tasks import ChatTasksMixin, current_tasks, needs_tasks
from app.services.origin_context import (
    FIELD_LABELS,
    OriginContextStore,
    OriginTransition,
    invalid,
    missing_conditions,
    simple_origin_reply,
)
from app.services.origin_matching import compact
from app.services.pending import ConversationOutcome, PendingFailure, PendingService

FOLLOWUP_TOPICS = {
    "recommendation_reason": "당시 추천 이유",
    "travel_time": "현재 자동차 이동시간",
    "parking": "현재 등록 주차 정보",
    "population": "현재 인구 현황",
}


class RecommendationChatProcessor(ChatTasksMixin):
    def __init__(
        self,
        model,
        kakao,
        recommendations,
        *,
        execution,
        contexts=None,
        reasons=None,
        current_answers=None,
        origin_search=None,
        origin_contexts=None,
    ):
        self.model, self.kakao, self.recommendations = model, kakao, recommendations
        self.contexts, self.reasons = contexts, reasons
        self.execution = execution
        self.current_answers = current_answers
        self.origin_search, self.origin_contexts = origin_search, origin_contexts

    @staticmethod
    def response(request, kind, message, **values):
        return ChatData(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind=kind,
            message=message,
            condition_update=ConditionUpdate(
                applied=False,
                conditions=request.current_conditions.model_copy(deep=True),
                version=request.condition_version,
                changes=[],
            ),
            stages=ChatStages(interpretation="ok"),
            result=None,
            clarification=values.pop("clarification", None),
            warnings=values.pop("warnings", []),
            **values,
        )

    async def process(self, request):
        request = request.model_copy(deep=True)
        stored, origin_draft, interpretation = None, None, "skipped"
        transition = OriginTransition()

        async def guard():
            nonlocal stored, origin_draft
            token = OriginContextStore.request_token(request)
            if token is not None:
                self.require_origin_services()
                origin_draft = self.origin_contexts.resolve(token, request)
            if request.target is not None:
                if self.contexts is None:
                    raise ServiceError(
                        "SERVICE_UNAVAILABLE",
                        "추천 문맥 검증 기능이 연결되지 않았습니다.",
                        retryable=False,
                    )
                stored = await self.contexts.resolve(
                    request.target.context_token,
                    trip_id=request.trip_id,
                    result_id=request.target.result_id,
                    place_id=request.target.place_id,
                )

        def rejected(original, error):
            failure = self.failure(original, error, interpretation=interpretation)
            return ConversationOutcome(failure.data, failure)

        async def compute(work):
            nonlocal interpretation
            try:
                transition.before = origin_draft
                data = await self.prepare(request, work, stored, origin_draft, transition)
                draft = current_draft(request, work)
                if data.kind == "answer" and draft is not None:
                    draft.paused = True
                    if data.answer.topic != "conversation":
                        draft.unanswered_turns += 1
                    data.pending_update = "clear"
                outcome = ConversationOutcome(data, targets=tuple(work.answered_targets))
            except PendingFailure as exc:
                outcome = ConversationOutcome(exc.data, exc, tuple(work.answered_targets))
            interpretation = outcome.response.stages.interpretation
            # Recheck after provider work, including accepted-condition failures.
            self.verify_target(request, stored)
            if origin_draft is not None:
                self.origin_contexts.resolve(origin_draft.token, request)
            # Validate before persistence, not only after process() returns to HTTP.
            PendingService.validate_context(outcome.response, request, retry=False)
            return outcome

        def publish(outcome):
            if self.origin_contexts is not None:
                self.origin_contexts.publish(transition, request, outcome)

        async def replay_guard(outcome, work):
            for target in outcome.targets:
                if target != request.target:
                    await self.contexts.resolve(
                        target.context_token,
                        trip_id=request.trip_id,
                        result_id=target.result_id,
                        place_id=target.place_id,
                    )
            question = outcome.response.clarification
            if question is not None and question.origin_context is not None:
                self.require_origin_services()
                draft = self.origin_contexts.resolve(question.origin_context.context_token, request)
                await self.check_origin_current(draft, request, work)
                if draft.clarification() != question:
                    raise invalid()
            # Completed selections replay their accepted update without comparing
            # it to today's conditions. Their input token must still be valid.
            if origin_draft is not None:
                self.origin_contexts.resolve(origin_draft.token, request)
            self.verify_target(request, stored)

        try:
            outcome = await self.execution.run(
                request,
                "chat",
                compute,
                rejected,
                require_current=False,
                guard=guard,
                publish=publish,
                replay_guard=replay_guard,
            )
        finally:
            if self.origin_contexts is not None:
                self.origin_contexts.discard(transition)
        if outcome.failure is not None:
            exc = outcome.failure
            raise PendingFailure(
                exc.code,
                exc.message,
                data=outcome.response,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return outcome.response

    async def check_origin_current(self, draft, request, work):
        self.origin_contexts.check_current(draft, request)
        await self.execution.check_current(request, work)
        # A DB read can outlive the origin question, too.
        self.origin_contexts.check_current(draft, request)

    def verify_target(self, request, stored):
        if request.target is not None:
            self.contexts.verify_loaded(
                stored,
                request.target.context_token,
                trip_id=request.trip_id,
                result_id=request.target.result_id,
                place_id=request.target.place_id,
            )

    async def prepare(self, request, work, stored, origin_draft, transition):
        """Interpret once inside the outer chat execution; never acquire its lock again."""
        selected_place = None
        if request.target is not None:
            selected = next(
                card for card in stored.cards if card.place_id == request.target.place_id
            )
            selected_place = {"name": selected.place_name, "kind": stored.result.kind}
        stage = "skipped"
        try:
            if origin_draft is not None and request.target is None:
                await self.check_origin_current(origin_draft, request, work)
            fast = (
                simple_origin_reply(origin_draft, request.message)
                if origin_draft and request.target is None
                else None
            )
            if request.origin_action is not None:
                parsed = Interpretation(intent="recommendation")
            elif fast == "ambiguous":
                data = self.origin_question(request, origin_draft)
                data.stages.interpretation = "skipped"
                return data
            elif fast is not None:
                parsed = Interpretation(intent="recommendation", origin_reply=fast)
            else:
                model_request = request.model_copy(deep=True)
                draft = current_draft(request, work) if origin_draft is None else None
                if origin_draft is not None:
                    model_request.pending_clarification = origin_draft.clarification()
                elif model_request.pending_clarification is None or is_condition_question(
                    model_request.pending_clarification
                ):
                    model_request.pending_clarification = draft.clarification if draft else None
                stage = "failed"
                parsed = await interpret(
                    self.model,
                    model_request,
                    selected_place=selected_place,
                    origin_confirmation=origin_draft.interpretation_context()
                    if origin_draft
                    else None,
                    condition_dialogue=draft.context() if draft else None,
                    task_dialogue=current_tasks(request, work).context()
                    if current_tasks(request, work)
                    else None,
                )
                stage = "ok"
            self.verify_target(request, stored)
            request = request.model_copy(deep=True)
            if origin_draft is None and (
                request.pending_clarification is None
                or is_condition_question(request.pending_clarification)
            ):
                draft = current_draft(request, work)
                request.pending_clarification = draft.clarification if draft else None
            data = await self.dispatch(
                request, parsed, parsed.patch(), stored, work, origin_draft, transition
            )
            data.stages.interpretation = stage
            return data
        except PendingFailure as exc:
            exc.data.stages.interpretation = stage
            raise
        except ServiceError as exc:
            raise self.failure(request, exc, interpretation=stage) from None
        except SQLAlchemyError:
            raise self.failure(
                request,
                ServiceError("DATA_UNAVAILABLE", "추천 상태를 조회할 수 없습니다."),
                interpretation=stage,
            ) from None
        except TimeoutError:
            raise self.failure(
                request,
                ServiceError("PROVIDER_TIMEOUT", "추천 처리 시간이 초과되었습니다."),
                interpretation=stage,
            ) from None

    def failure(self, request, error, *, interpretation):
        data = self.response(request, "failure", error.message)
        data.stages.interpretation = interpretation
        return PendingFailure(
            error.code,
            error.message,
            data=data,
            retryable=error.retryable,
            retry_after_seconds=error.retry_after_seconds,
        )

    def clarify_target(self, request, intent):
        topic = FOLLOWUP_TOPICS.get(intent, "정보")
        question = f"{topic}에 대해 답할 수 있도록 대상 카드를 선택해 주세요."
        return self.response(
            request,
            "clarification",
            question,
            clarification=Clarification(
                question=question,
                required_fields=["target"],
                pending_patch=ConditionsPatch(),
                choices=[],
            ),
        )

    def clarify_intent(self, request, question=None):
        question = question or "장소에 대해 질문할지, 조건을 바꿔 새 추천을 받을지 알려 주세요."
        return self.response(
            request,
            "clarification",
            question,
            clarification=Clarification(
                question=question,
                required_fields=["intent"],
                pending_patch=ConditionsPatch(),
                choices=[],
            ),
        )

    def conversation_notice(self, request, message):
        return self.response(
            request,
            "answer",
            message,
            pending_update="clear",
            answer=Answer(
                topic="conversation",
                place_id=None,
                text=message,
                original_result_id=None,
                original_recommended_at=None,
                checked_at=None,
                information=[],
            ),
        )

    async def dispatch(
        self, request, parsed, extracted_patch, stored, work, origin_draft, transition
    ):
        if (
            parsed.intent == "intent_clarification"
            and not parsed.information_requests
            and not parsed.recommendation_requested
        ):
            return self.clarify_intent(request, parsed.question)
        if needs_tasks(request, parsed, work):
            return await self.dispatch_tasks(
                request, parsed, stored, work, origin_draft, transition
            )
        return await self.dispatch_single(
            request, parsed, extracted_patch, stored, work, origin_draft, transition
        )

    async def dispatch_single(
        self, request, parsed, extracted_patch, stored, work, origin_draft, transition
    ):
        if origin_draft is None and parsed.dialogue_action in {"cancel", "pause", "restart"}:
            await self.execution.check_current(request, work)
            if parsed.dialogue_action == "cancel":
                work.condition_draft = None
                return self.conversation_notice(
                    request, "진행 중이던 조건 변경을 취소했어요. 기존 조건과 추천은 유지돼요."
                )
            if parsed.dialogue_action == "pause":
                draft = current_draft(request, work)
                if draft is not None or extracted_patch.model_fields_set or parsed.headcount_hint:
                    work.condition_draft, _ = resolve_conditions(request, parsed, draft)
                    work.condition_draft.paused = True
                return self.conversation_notice(
                    request,
                    "조건 확인은 잠시 보류할게요. "
                    "이어서 말씀하시면 보관한 초안에서 계속할 수 있어요.",
                )
            work.condition_draft = None
            request.pending_clarification = None
            if not extracted_patch.model_fields_set and not parsed.origin_name:
                return self.conversation_notice(
                    request, "진행 중이던 변경 초안을 비웠어요. 새로 원하는 조건을 알려 주세요."
                )
        if parsed.intent == "intent_clarification":
            return self.clarify_intent(request, parsed.question)
        if parsed.target_ambiguous and parsed.intent in FOLLOWUP_TOPICS:
            return self.clarify_target(request, parsed.intent)
        if parsed.intent in FOLLOWUP_TOPICS:
            if (
                parsed.origin_name
                or parsed.origin_reply
                or extracted_patch.model_fields_set
                or parsed.headcount_hint
            ):
                return self.clarify_intent(request)
            if request.target is None:
                return self.clarify_target(request, parsed.intent)
            if parsed.intent == "recommendation_reason":
                if self.reasons is None:
                    raise ServiceError(
                        "SERVICE_UNAVAILABLE",
                        "당시 추천 이유 답변이 연결되지 않았습니다.",
                        retryable=False,
                    )
                data = self.reasons.answer_from_context(request, stored)
                data.stages.interpretation = "ok"
                return data
            if self.current_answers is None:
                raise ServiceError(
                    "SERVICE_UNAVAILABLE",
                    "현재 정보 조회가 연결되지 않았습니다.",
                    retryable=False,
                )
            answer = await self.current_answers.answer(request, parsed.intent, stored)
            data = self.response(request, "answer", answer.text, answer=answer)
            if parsed.intent == "travel_time":
                data.stages.routes = "ok"
            return data
        if parsed.intent == "scope_notice":
            message = (
                "현재 출발하는 자동차 이동 기준 장소 추천을 지원합니다. "
                "요청한 정보는 보장할 수 없습니다."
            )
            return self.response(
                request,
                "answer",
                message,
                answer=Answer(
                    topic="scope_notice",
                    place_id=None,
                    text=message,
                    original_result_id=None,
                    original_recommended_at=None,
                    checked_at=None,
                    information=[],
                ),
            )
        if origin_draft is not None or (
            parsed.origin_name and not self.keeps_origin(request, parsed)
        ):
            return await self.prepare_origin(
                request, parsed, extracted_patch, work, origin_draft, transition
            )
        await self.execution.check_current(request, work)
        previous = current_draft(request, work)
        answered = previous is not None and bool(
            set(previous.clarification.required_fields) & extracted_patch.model_fields_set
        )
        draft, warnings = resolve_conditions(request, parsed, previous)
        question = draft.clarification
        if question.required_fields or (
            parsed.intent == "clarification"
            and parsed.dialogue_action != "proceed"
            and not answered
        ):
            work.condition_draft = draft
            return self.response(
                request,
                "clarification",
                question.question,
                clarification=question,
            )
        try:
            data = await self.recommend(request, question.pending_patch, work)
        except PendingFailure as exc:
            if exc.data.condition_update.applied:
                exc.data.warnings.extend(warnings)
                exc.data.message = " ".join(w.message for w in warnings) + " " + exc.data.message
            raise
        data.warnings.extend(warnings)
        if warnings:
            data.message = " ".join(w.message for w in warnings) + " " + data.message
        return data

    async def recommend(self, request, patch, work, origin_draft=None):
        await self.execution.check_current(request, work)
        if origin_draft is not None:
            self.origin_contexts.check_current(origin_draft, request)
        structured = RecommendationRequest(
            **request.model_dump(
                include={
                    "request_id",
                    "trip_id",
                    "condition_version",
                    "current_conditions",
                    "excluded_place_ids",
                }
            ),
            conditions_patch=patch,
        )
        outcome = await self.recommendations.prepare(structured, work)
        values = outcome.response.model_dump()
        values["stages"]["interpretation"] = "ok"
        data = ChatData.model_validate(values)
        if outcome.failure:
            exc = outcome.failure
            raise PendingFailure(
                exc.code,
                exc.message,
                data=data,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return data

    def require_origin_services(self):
        if self.origin_search is None or self.origin_contexts is None:
            raise ServiceError(
                "SERVICE_UNAVAILABLE", "출발지 확인 기능이 연결되지 않았습니다.", retryable=False
            )

    @staticmethod
    def keeps_origin(request, parsed):
        origin = request.current_conditions.origin
        return (
            origin is not None
            and compact(origin.label) == compact(parsed.origin_name)
            and not any(h.source == "message" for h in parsed.origin_hints)
        )

    def origin_question(self, request, draft):
        clarification = draft.clarification()
        return self.response(
            request,
            "clarification",
            clarification.question,
            clarification=clarification,
            warnings=list(draft.result.warnings),
        )

    async def prepare_origin(self, request, parsed, extracted, work, draft, transition):
        self.require_origin_services()
        await self.execution.check_current(request, work)
        if draft is not None:
            self.origin_contexts.check_current(draft, request)
            patch = draft.patch.model_dump(exclude_unset=True)
            pending_fields = draft.waiting_fields
        else:
            pending = request.pending_clarification
            pending_fields = (
                pending.required_fields
                if pending and set(pending.required_fields) <= set(Conditions.model_fields)
                else ()
            )
            patch = (
                pending.pending_patch.model_dump(exclude_unset=True)
                if pending and set(pending.required_fields) <= set(Conditions.model_fields)
                else {}
            )
        changed = extracted.model_dump(exclude_unset=True)
        patch.update(changed)
        waiting = set(pending_fields) - set(changed)
        waiting.update(parsed.clarification_fields)
        waiting.discard("origin")
        if parsed.origin_name:
            patch.pop("origin", None)
            result = await self.origin_search.search(parsed.origin_name, parsed.origin_hints)
            # Search may wait on providers; do not publish a draft for obsolete conditions.
            await self.execution.check_current(request, work)
            if draft is not None:
                self.origin_contexts.check_current(draft, request)
            transition.after = self.origin_contexts.reserve(
                request,
                result,
                ConditionsPatch.model_validate(patch),
                previous=draft,
                new_search=True,
                waiting_fields=waiting,
            )
            return self.origin_question(request, transition.after)
        if draft is None:
            raise invalid()
        result, selected = draft.result, draft.selected_id
        action = (
            request.origin_action.action
            if request.origin_action
            else parsed.origin_reply.action
            if parsed.origin_reply
            else None
        )
        if action in {"restart", "show_alternatives"}:
            patch.pop("origin", None)
            selected = None
            if action == "show_alternatives" and len(result.candidates) > 1:
                result = replace(result, mode="choose")
                question = None
            else:
                result = replace(result, candidates=(), mode="refine", reason_code=None)
                question = "다른 출발지의 장소명이나 주소를 입력해 주세요."
            transition.after = self.origin_contexts.reserve(
                request,
                result,
                ConditionsPatch.model_validate(patch),
                previous=draft,
                waiting_fields=waiting,
                question=question,
            )
            return self.origin_question(request, transition.after)
        if action in {"select", "confirm"}:
            if request.origin_action:
                identifier = request.origin_action.candidate_id
            else:
                index = 1 if action == "confirm" else parsed.origin_reply.candidate_index
                if action == "confirm" and result.mode != "confirm":
                    raise invalid()
                if index > len(result.candidates):
                    raise invalid()
                identifier = result.candidates[index - 1].candidate_id
            candidate = draft.candidate(identifier)
            selected = candidate.candidate_id
            patch["origin"] = candidate.location.model_copy(deep=True)
        if selected is None:
            transition.after = self.origin_contexts.reserve(
                request,
                result,
                ConditionsPatch.model_validate(patch),
                previous=draft,
                waiting_fields=waiting,
                question=draft.question,
            )
            return self.origin_question(request, transition.after)
        patch = ConditionsPatch.model_validate(patch)
        merged = Conditions.model_validate(
            request.current_conditions.model_dump() | patch.model_dump(exclude_unset=True)
        )
        required = list(dict.fromkeys(missing_conditions(merged) + sorted(waiting)))
        if required or parsed.intent == "clarification":
            question = parsed.question or (
                ", ".join(FIELD_LABELS[field] for field in required) + "을 알려 주세요."
                if required
                else "추가 조건을 확인해 주세요."
            )
            transition.after = self.origin_contexts.reserve(
                request,
                result,
                patch,
                previous=draft,
                selected_id=selected,
                waiting_fields=required,
                question=question,
            )
            return self.origin_question(request, transition.after)
        return await self.recommend(request, patch, work, origin_draft=draft)
