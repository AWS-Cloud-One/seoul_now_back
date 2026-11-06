"""R01 wire contract. Candidate/card internals remain a provisional payload."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
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
    keyword: str | None = Field(default=None, min_length=1, max_length=100)


class Conditions(ContractModel):
    origin: Location | None
    purposes: list[Purpose] = Field(max_length=2)
    group_type: GroupType | None
    age_groups: list[AgeGroup] | None
    headcount: PositiveInt | None
    max_travel_seconds: PositiveInt | None

    @field_validator("purposes")
    @classmethod
    def unique_purposes(cls, values):
        if len({p.purpose for p in values}) != len(values):
            raise ValueError("목적은 중복할 수 없습니다.")
        return values

    @field_validator("age_groups")
    @classmethod
    def unique_ages(cls, values):
        if values is not None and (not values or len(set(values)) != len(values)):
            raise ValueError("연령대는 중복 없는 1개 이상이어야 합니다.")
        return values


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


class RecommendationRequest(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "example": {
                "request_id": "11111111-1111-4111-8111-111111111111",
                "trip_id": "22222222-2222-4222-8222-222222222222",
                "condition_version": 0,
                "current_conditions": {
                    "origin": None,
                    "purposes": [],
                    "group_type": None,
                    "age_groups": None,
                    "headcount": None,
                    "max_travel_seconds": None,
                },
                "excluded_place_ids": [],
                "conditions_patch": {
                    "origin": {
                        "label": "시청역",
                        "coordinates": {"latitude": 37.5657, "longitude": 126.977},
                        "address": None,
                        "region": None,
                        "supported": False,
                    },
                    "purposes": [{"purpose": "food", "keyword": "한식집"}],
                    "group_type": "family",
                    "max_travel_seconds": 1800,
                },
            }
        },
    )

    request_id: UUID
    trip_id: UUID
    condition_version: int = Field(strict=True, ge=0)
    current_conditions: Conditions
    excluded_place_ids: list[str]
    conditions_patch: ConditionsPatch

    @field_validator("excluded_place_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not v.strip() or not v.startswith("kakao:") or not v[6:].strip() for v in values):
            raise ValueError("장소 ID는 kakao:<id> 형식이어야 합니다.")
        return list(dict.fromkeys(values))


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
