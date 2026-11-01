"""R01 wire contract. Candidate/card internals remain a provisional payload."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.schemas.common import ApiEnvelope

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
GroupType = Literal["solo", "date", "friends", "family", "other"]
AgeGroup = Literal["under_10", "teens", "twenties", "thirties", "forties", "fifties", "sixty_plus"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Coordinates(WireModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Location(WireModel):
    label: str
    coordinates: Coordinates
    address: str | None
    region: Literal["seoul", "incheon", "gyeonggi", "other"] | None
    supported: bool = Field(strict=True)


class Purpose(WireModel):
    purpose: Literal["cafe", "food", "walk", "exhibition"]
    keyword: str | None = Field(default=None, max_length=100)


class Conditions(WireModel):
    origin: Location | None
    purposes: list[Purpose] = Field(max_length=2)
    group_type: GroupType | None
    age_groups: list[AgeGroup] | None
    headcount: PositiveInt | None
    max_travel_seconds: PositiveInt | None


class ConditionsPatch(WireModel):
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


class RecommendationRequest(WireModel):
    request_id: UUID
    trip_id: UUID
    condition_version: int = Field(strict=True, ge=0)
    current_conditions: Conditions
    excluded_place_ids: list[str]
    conditions_patch: ConditionsPatch


class Change(WireModel):
    field: str
    before: JsonValue
    after: JsonValue


class ConditionUpdate(WireModel):
    applied: bool
    conditions: Conditions
    version: int = Field(ge=0)
    changes: list[Change]


class Choice(WireModel):
    label: str
    value: JsonValue


class Clarification(WireModel):
    question: str
    required_fields: list[str]
    pending_patch: ConditionsPatch
    choices: list[Choice]


class Stages(WireModel):
    interpretation: Literal["skipped"] = "skipped"
    routes: Literal["skipped", "ok", "partial", "failed"] = "skipped"
    explanation: Literal["skipped", "ok", "fallback"] = "skipped"


class WarningMessage(WireModel):
    code: str
    message: str


class RecommendationData(WireModel):
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


class RecommendationEnvelope(ApiEnvelope[RecommendationData]):
    pass
