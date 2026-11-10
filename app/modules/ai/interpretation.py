"""Natural-language extraction uses current Conditions fields, not a second public API."""

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.core.exceptions import ServiceError
from app.schemas.common import ContractModel
from app.schemas.origin import OriginHint, OriginInterpretationContext, OriginReply
from app.schemas.recommendations import AgeGroup, ConditionsPatch, GroupType, PositiveInt, Purpose


class HeadcountHint(ContractModel):
    minimum: PositiveInt | None
    quote: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def literal_minimum(self):
        if self.minimum is None:
            return self
        matches = re.findall(r"(?<![\d.\-])([0-9]+)\s*명\s*이상|최소\s*([0-9]+)\s*명", self.quote)
        if len(matches) != 1 or int(next(value for value in matches[0] if value)) != self.minimum:
            raise ValueError("인원 기준값은 사용자가 명시한 최소 인원이어야 합니다.")
        return self


class InformationRequest(ContractModel):
    topic: Literal["recommendation_reason", "travel_time", "parking", "population", "scope_notice"]
    target: Literal["selected", "unresolved", "new_result"] = "selected"
    quote: str = Field(min_length=1, max_length=120)

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
    information_requests: list[InformationRequest] = Field(default_factory=list, max_length=5)
    recommendation_requested: bool = False
    recommendation_quote: str | None = Field(default=None, max_length=120)
    request_relation: Literal[
        "independent", "information_first", "recommendation_first", "conditional"
    ] = "independent"
    relation_quote: str | None = Field(default=None, max_length=120)
    resume_information: bool = False
    resume_quote: str | None = Field(default=None, max_length=120)
    dialogue_scope: Literal["conditions", "information", "all"] = "conditions"
    dialogue_action: Literal["continue", "pause", "cancel", "restart", "proceed"] = "continue"
    action_quote: str | None = Field(default=None, min_length=1, max_length=500)
    headcount_hint: HeadcountHint | None = None
    origin_name: str | None = Field(default=None, min_length=1, max_length=100)
    origin_hints: list[OriginHint] = Field(default_factory=list, max_length=10)
    origin_reply: OriginReply | None = None
    purposes: list[Purpose] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
        description="이번 발언에서 방문 목적 또는 검색어를 새로 지정/변경할 때만 값. "
        "시간·인원 답변, 정보 질문, 취소·보류·진행만 요청하면 반드시 null. "
        "현재 목적을 복사하지 않는다.",
    )
    group_type: GroupType | None = None
    age_groups: list[AgeGroup] | None = Field(default=None, min_length=1)
    headcount: PositiveInt | None = None
    max_travel_seconds: PositiveInt | None = None
    clear_fields: list[Literal["group_type", "age_groups", "headcount", "max_travel_seconds"]] = Field(
        default_factory=list
    )
    question: str | None = None
    clarification_fields: list[
        Literal["origin", "purposes", "group_type", "age_groups", "headcount", "max_travel_seconds"]
    ] = Field(
        default_factory=list,
        max_length=6,
        description="이번 발언에서 새로 확인이 필요한 항목만. "
        "이전 질문은 서버가 유지하므로 복사하지 않는다.",
    )

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
                "information_requests",
                "recommendation_requested",
                "recommendation_quote",
                "request_relation",
                "relation_quote",
                "resume_information",
                "resume_quote",
                "dialogue_scope",
                "target_ambiguous",
                "dialogue_action",
                "action_quote",
                "headcount_hint",
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
    condition_dialogue=None,
    task_dialogue=None,
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
                    "condition_dialogue": condition_dialogue,
                    "task_dialogue": task_dialogue,
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
        # A stored prerequisite is context, not a newly imposed condition. In
        # particular, do not re-arm it when the user explicitly proceeds without it.
        if (
            parsed.request_relation != "independent"
            and parsed.relation_quote
            and task_dialogue is not None
            and parsed.relation_quote == task_dialogue.get("condition_quote")
            and parsed.relation_quote not in request.message
        ):
            parsed.request_relation = "independent"
            parsed.relation_quote = None
        quotes = [item.quote for item in parsed.information_requests]
        if parsed.recommendation_requested:
            quotes.append(parsed.recommendation_quote)
        if parsed.request_relation != "independent":
            quotes.append(parsed.relation_quote)
        if parsed.resume_information:
            quotes.append(parsed.resume_quote)
        if any(not quote or quote not in request.message for quote in quotes):
            raise ValueError("복수 작업의 근거는 이번 사용자 발언이어야 합니다.")
        if parsed.dialogue_action != "continue" and (
            not parsed.action_quote or parsed.action_quote not in request.message
        ):
            raise ValueError("대화 작업의 근거는 이번 사용자 발언이어야 합니다.")
        # Some models echo the server's old clue despite delta-only instructions.
        # It is already verified and stored; do not mistake an echo for new evidence.
        if (
            parsed.headcount_hint is not None
            and parsed.headcount_hint.quote not in request.message
            and condition_dialogue is not None
            and parsed.headcount_hint.model_dump() == condition_dialogue.get("headcount_hint")
        ):
            parsed.headcount_hint = None
        if parsed.headcount_hint is not None and (
            parsed.headcount_hint.quote not in request.message
            or parsed.headcount is not None
            or "headcount" in parsed.clear_fields
        ):
            raise ValueError("인원 범위를 정확한 인원 또는 조건 해제와 섞지 않습니다.")
        return parsed
    except ValueError, TypeError:
        raise ServiceError(
            "AI_INTERPRETATION_FAILED", "질문 의도와 조건 변경을 확인할 수 없습니다."
        ) from None
