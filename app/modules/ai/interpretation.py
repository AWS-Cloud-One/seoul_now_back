"""Natural-language extraction uses current Conditions fields, not a second public API."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core.exceptions import ServiceError
from app.schemas.common import ContractModel
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
    purposes: list[Purpose] | None = Field(default=None, min_length=1, max_length=2)
    group_type: GroupType | None = None
    age_groups: list[AgeGroup] | None = Field(default=None, min_length=1)
    headcount: PositiveInt | None = None
    max_travel_seconds: PositiveInt | None = None
    clear_fields: list[Literal["group_type", "age_groups", "headcount", "max_travel_seconds"]] = (
        Field(default_factory=list)
    )
    question: str | None = None

    def patch(self):
        values = self.model_dump(
            exclude={"intent", "target_ambiguous", "origin_name", "clear_fields", "question"},
            exclude_none=True,
        )
        if set(values) & set(self.clear_fields):
            raise ServiceError(
                "AI_INTERPRETATION_FAILED", "조건 변경과 해제를 함께 지정할 수 없습니다."
            )
        values.update({field: None for field in self.clear_fields})
        return ConditionsPatch.model_validate(values)


async def interpret(model, request, *, selected_place=None):
    try:
        return Interpretation.model_validate(
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
                    "pending_clarification": request.pending_clarification.model_dump(mode="json")
                    if request.pending_clarification
                    else None,
                },
                Interpretation,
            )
        )
    except ValueError, TypeError:
        raise ServiceError(
            "AI_INTERPRETATION_FAILED", "질문 의도와 조건 변경을 확인할 수 없습니다."
        ) from None
