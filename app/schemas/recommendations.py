"""R01 wire contract. Candidate/card internals remain a provisional payload."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from app.schemas.common import ApiEnvelope, ContractModel
from app.schemas.location import Location

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
GroupType = Literal["solo", "date", "friends", "family", "other"]
AgeGroup = Literal["under_10", "teens", "twenties", "thirties", "forties", "fifties", "sixty_plus"]


class Purpose(ContractModel):
    purpose: Literal["cafe", "food", "walk", "exhibition"]
    keyword: str | None = Field(default=None, max_length=100)


class Conditions(ContractModel):
    origin: Location | None
    purposes: list[Purpose] = Field(max_length=2)
    group_type: GroupType | None
    age_groups: list[AgeGroup] | None
    headcount: PositiveInt | None
    max_travel_seconds: PositiveInt | None


class ConditionsPatch(ContractModel):
    origin: Location | None = None
    purposes: list[Purpose] | None = Field(default=None, min_length=1, max_length=2)
    group_type: GroupType | None = None
    age_groups: list[AgeGroup] | None = None
    headcount: PositiveInt | None = None
    max_travel_seconds: PositiveInt | None = None

    @field_validator("origin", "purposes")
    @classmethod
    def cannot_clear_required_condition(cls, value):
        if value is None:
            raise ValueError("origin과 purposes는 명시적 null로 해제할 수 없습니다.")
        return value


class RequestContext(ContractModel):
    request_id: UUID
    trip_id: UUID
    condition_version: int = Field(strict=True, ge=0)
    current_conditions: Conditions
    excluded_place_ids: list[str]


class RecommendationRequest(RequestContext):
    conditions_patch: ConditionsPatch


class Change(ContractModel):
    field: str
    before: JsonValue
    after: JsonValue


class ConditionUpdate(ContractModel):
    applied: bool
    conditions: Conditions
    version: int = Field(ge=0)
    changes: list[Change]


class Choice(ContractModel):
    label: str
    value: JsonValue


class Clarification(ContractModel):
    question: str
    required_fields: list[str]
    pending_patch: ConditionsPatch
    choices: list[Choice]

    @field_serializer("pending_patch")
    def serialize_pending_patch(self, value, info):
        # Omitted means unchanged; explicit null means clear. Preserve both on round-trip.
        return value.model_dump(mode=info.mode, exclude_unset=True)


class Stages(ContractModel):
    interpretation: Literal["skipped"] = "skipped"
    routes: Literal["skipped", "ok", "partial", "failed"] = "skipped"
    explanation: Literal["skipped", "ok", "fallback"] = "skipped"


class WarningMessage(ContractModel):
    code: str
    message: str


class RecommendationResponse(ContractModel):
    trip_id: UUID
    base_condition_version: int
    kind: Literal[
        "clarification", "recommendation", "general_candidates", "no_candidates", "failure"
    ]
    message: str
    condition_update: ConditionUpdate
    stages: Stages
    # Final card fields and historical context tokens are not settled by R01.
    result: dict[str, JsonValue] | None
    clarification: Clarification | None
    answer: None = None
    warnings: list[WarningMessage]

    @model_validator(mode="after")
    def matching_payload(self):
        has_result = self.kind in {"recommendation", "general_candidates"}
        if (self.result is not None) != has_result:
            raise ValueError("result는 추천 또는 일반 후보에서만 필요합니다.")
        if has_result:
            cards = self.result.get("cards")
            if not isinstance(cards, list) or not 1 <= len(cards) <= 3:
                raise ValueError("추천 결과에는 1~3개 카드가 필요합니다.")
        if (self.clarification is not None) != (self.kind == "clarification"):
            raise ValueError("clarification은 추가 질문에서만 필요합니다.")
        return self


class RecommendationEnvelope(ApiEnvelope[RecommendationResponse]):
    pass
