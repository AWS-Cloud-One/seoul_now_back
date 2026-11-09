"""Natural-language extraction uses current Conditions fields, not a second public API."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.core.exceptions import ServiceError
from app.schemas.common import ContractModel
from app.schemas.origin import OriginHint, OriginInterpretationContext, OriginReply
from app.schemas.recommendations import AgeGroup, ConditionsPatch, GroupType, PositiveInt, Purpose


class Interpretation(ContractModel):
    intent: Literal[
        "recommendation",
        "clarification",
        "scope_notice",
        "recommendation_reason",
        "travel_time",
        "parking",
        "population",
        "intent_clarification",
    ]
    target_ambiguous: bool = False
    origin_name: str | None = Field(default=None, min_length=1, max_length=100)
    origin_hints: list[OriginHint] = Field(default_factory=list, max_length=10)
    origin_reply: OriginReply | None = None
    purposes: list[Purpose] | None = Field(default=None, min_length=1, max_length=2)
    group_type: GroupType | None = None
    age_groups: list[AgeGroup] | None = Field(default=None, min_length=1)
    headcount: PositiveInt | None = None
    max_travel_seconds: PositiveInt | None = None
    clear_fields: list[Literal["group_type", "age_groups", "headcount", "max_travel_seconds"]] = (
        Field(default_factory=list)
    )
    question: str | None = None
    clarification_fields: list[
        Literal["origin", "purposes", "group_type", "age_groups", "headcount", "max_travel_seconds"]
    ] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def origin_operation(self):
        if self.origin_reply is not None:
            if self.intent not in {"recommendation", "clarification"}:
                raise ValueError("출발지 선택은 추천 조건 처리에서만 가능합니다.")
            if self.origin_name is not None and self.origin_reply.action != "restart":
                raise ValueError("기존 후보 선택과 새 출발지 검색을 함께 지정할 수 없습니다.")
        return self

    def patch(self):
        values = self.model_dump(
            exclude={
                "intent",
                "target_ambiguous",
                "origin_name",
                "origin_hints",
                "origin_reply",
                "clear_fields",
                "question",
                "clarification_fields",
            },
            exclude_none=True,
        )
        if set(values) & set(self.clear_fields):
            raise ServiceError(
                "AI_INTERPRETATION_FAILED", "조건 변경과 해제를 함께 지정할 수 없습니다."
            )
        values.update({field: None for field in self.clear_fields})
        return ConditionsPatch.model_validate(values)


def validate_origin_evidence(parsed, request, origin_confirmation):
    for hint in parsed.origin_hints:
        if hint.source == "message":
            sources = [request.message]
        elif hint.source == "recent_user_message":
            if hint.message_index >= len(request.recent_messages):
                raise ValueError("이전 발언의 인덱스가 범위를 벗어났습니다.")
            message = request.recent_messages[hint.message_index]
            if message.role != "user":
                raise ValueError("assistant의 추측을 사용자 출발지 단서로 사용할 수 없습니다.")
            sources = [message.content]
        else:
            origin = request.current_conditions.origin
            sources = [origin.label, origin.address] if origin is not None else []
        if not any(source and hint.quote in source for source in sources):
            raise ValueError("출발지 단서의 출처에서 인용을 확인할 수 없습니다.")
    reply = parsed.origin_reply
    if reply is not None:
        if origin_confirmation is None:
            raise ValueError("검증된 출발지 확인 문맥이 없습니다.")
        if origin_confirmation.mode == "confirmed" and reply.action in {"confirm", "select"}:
            raise ValueError("확인된 출발지를 다시 선택하는 응답이 아닙니다.")
        if reply.action == "confirm" and origin_confirmation.mode != "confirm":
            raise ValueError("여러 후보 중 하나를 특정해 주세요.")
        if reply.action == "select" and reply.candidate_index > len(origin_confirmation.candidates):
            raise ValueError("출발지 후보 번호가 범위를 벗어났습니다.")


def pending_for_interpretation(pending):
    if pending is None:
        return None
    # Caller-provided choice values may contain tokens/coordinates. They are not
    # authority for selection; verified display candidates travel separately.
    return pending.model_dump(mode="json", exclude={"choices", "origin_context"})


async def interpret(
    model,
    request,
    *,
    selected_place=None,
    origin_confirmation: OriginInterpretationContext | None = None,
):
    try:
        parsed = Interpretation.model_validate(
            await model.complete(
                Path(__file__)
                .with_name("prompts")
                .joinpath("interpretation.txt")
                .read_text("utf-8"),
                {
                    "message": request.message,
                    # Only a verified display label/kind, never a bearer token or result IDs.
                    "selected_place": selected_place,
                    "current_conditions": request.current_conditions.model_dump(mode="json"),
                    "recent_messages": [
                        message.model_dump() for message in request.recent_messages
                    ],
                    "pending_clarification": pending_for_interpretation(
                        request.pending_clarification
                    ),
                    "origin_confirmation": origin_confirmation.model_dump(mode="json")
                    if origin_confirmation is not None
                    else None,
                },
                Interpretation,
            )
        )
        validate_origin_evidence(parsed, request, origin_confirmation)
        return parsed
    except ValueError, TypeError:
        raise ServiceError(
            "AI_INTERPRETATION_FAILED", "질문 의도와 조건 변경을 확인할 수 없습니다."
        ) from None
