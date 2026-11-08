"""C01 adapter: interpret once, then call the same structured recommendation service."""

from sqlalchemy.exc import SQLAlchemyError

from app.clients.kakao import KakaoError
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import interpret
from app.schemas.location import Coordinates, Location
from app.schemas.pending import Answer, ChatData, ChatStages
from app.schemas.recommendations import (
    Clarification,
    Conditions,
    ConditionsPatch,
    ConditionUpdate,
    RecommendationRequest,
)
from app.services.locations import documents, provider_failure, text
from app.services.pending import ConversationOutcome, PendingFailure, PendingService

FOLLOWUP_TOPICS = {
    "recommendation_reason": "당시 추천 이유",
    "travel_time": "현재 자동차 이동시간",
    "parking": "현재 등록 주차 정보",
    "population": "현재 인구 현황",
}


class RecommendationChatProcessor:
    def __init__(self, model, kakao, recommendations, *, execution, contexts=None, reasons=None):
        self.model, self.kakao, self.recommendations = model, kakao, recommendations
        self.contexts, self.reasons = contexts, reasons
        self.execution = execution

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
            warnings=[],
            **values,
        )

    async def process(self, request):
        request = request.model_copy(deep=True)
        stored, interpretation = None, "skipped"

        async def guard():
            nonlocal stored
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
                data = await self.prepare(request, work, stored)
                outcome = ConversationOutcome(data)
            except PendingFailure as exc:
                outcome = ConversationOutcome(exc.data, exc)
            interpretation = outcome.response.stages.interpretation
            # Recheck after provider work, including accepted-condition failures.
            self.verify_target(request, stored)
            # Validate before persistence, not only after process() returns to HTTP.
            PendingService.validate_context(outcome.response, request, retry=False)
            return outcome

        outcome = await self.execution.run(
            request, "chat", compute, rejected, require_current=False, guard=guard
        )
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

    def verify_target(self, request, stored):
        if request.target is not None:
            self.contexts.verify_loaded(
                stored,
                request.target.context_token,
                trip_id=request.trip_id,
                result_id=request.target.result_id,
                place_id=request.target.place_id,
            )

    async def prepare(self, request, work, stored):
        """Interpret once inside the outer chat execution; never acquire its lock again."""
        selected_place = None
        if request.target is not None:
            selected = next(
                card for card in stored.cards if card.place_id == request.target.place_id
            )
            selected_place = {"name": selected.place_name, "kind": stored.result.kind}
        try:
            parsed = await interpret(self.model, request, selected_place=selected_place)
            extracted_patch = parsed.patch()
        except ServiceError as exc:
            raise self.failure(request, exc, interpretation="failed") from None
        except TimeoutError:
            raise self.failure(
                request,
                ServiceError("PROVIDER_TIMEOUT", "질문 해석 시간이 초과되었습니다."),
                interpretation="failed",
            ) from None
        try:
            self.verify_target(request, stored)
            return await self.dispatch(request, parsed, extracted_patch, stored, work)
        except PendingFailure:
            raise
        except ServiceError as exc:
            raise self.failure(request, exc, interpretation="ok") from None
        except SQLAlchemyError:
            raise self.failure(
                request,
                ServiceError("DATA_UNAVAILABLE", "추천 상태를 조회할 수 없습니다."),
                interpretation="ok",
            ) from None
        except TimeoutError:
            raise self.failure(
                request,
                ServiceError("PROVIDER_TIMEOUT", "추천 처리 시간이 초과되었습니다."),
                interpretation="ok",
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

    def clarify_intent(self, request):
        question = "장소에 대해 질문할지, 조건을 바꿔 새 추천을 받을지 알려 주세요."
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

    async def dispatch(self, request, parsed, extracted_patch, stored, work):
        if parsed.intent == "intent_clarification":
            return self.clarify_intent(request)
        if parsed.target_ambiguous:
            return self.clarify_target(request, parsed.intent)
        if parsed.intent in FOLLOWUP_TOPICS:
            if parsed.origin_name or extracted_patch.model_fields_set:
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
            # Part 1 owns current facts. Never turn these questions into a new recommendation.
            raise ServiceError(
                "SERVICE_UNAVAILABLE",
                f"{FOLLOWUP_TOPICS[parsed.intent]} 조회가 아직 연결되지 않았습니다.",
                retryable=False,
            )
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
        patch = (
            request.pending_clarification.pending_patch.model_dump(exclude_unset=True)
            if (
                request.pending_clarification
                and set(request.pending_clarification.required_fields)
                <= set(Conditions.model_fields)
            )
            else {}
        )
        patch.update(extracted_patch.model_dump(exclude_unset=True))
        unresolved_origin = False
        if parsed.origin_name:
            try:
                rows, _ = documents(await self.kakao.keyword(parsed.origin_name, page=1, size=5))
                exact = [row for row in rows if text(row.get("place_name")) == parsed.origin_name]
                if len(exact) == 1:
                    row = exact[0]
                    patch["origin"] = Location(
                        label=parsed.origin_name,
                        coordinates=Coordinates(latitude=row["y"], longitude=row["x"]),
                        address=text(row.get("address_name")),
                        region=None,
                        supported=False,
                    )
                else:
                    unresolved_origin = True
            except KakaoError as exc:
                raise provider_failure(exc) from None
            except ValueError, TypeError, KeyError:
                raise ServiceError(
                    "PROVIDER_ERROR", "출발지 검색 결과를 검증할 수 없습니다."
                ) from None
        patch = ConditionsPatch.model_validate(patch)
        merged = request.current_conditions.model_dump()
        merged.update(patch.model_dump(exclude_unset=True))
        conditions = Conditions.model_validate(merged)
        required = []
        if unresolved_origin or conditions.origin is None:
            required.append("origin")
        if not conditions.purposes:
            required.append("purposes")
        if required or parsed.intent == "clarification":
            message = parsed.question or "출발지와 추천 목적을 확인해 주세요."
            return self.response(
                request,
                "clarification",
                message,
                clarification=Clarification(
                    question=message, required_fields=required, pending_patch=patch, choices=[]
                ),
            )
        await self.execution.check_current(request, work)
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
