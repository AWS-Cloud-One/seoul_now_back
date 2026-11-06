"""Public chat contract and validated provider-independent recommendation types."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

RequestType = Literal["new_recommendation", "recommend_more", "update_conditions"]
Grade = Literal["원활", "서행", "정체", "확인 불가"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Conditions(StrictModel):
    query: str | None = Field(default=None, min_length=1, max_length=100)
    x: float | None = Field(default=None, ge=124, le=132)
    y: float | None = Field(default=None, ge=33, le=39)
    max_minutes: int = Field(default=30, ge=1, le=180, strict=True)

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be supplied together")
        return self


class Changes(StrictModel):
    query: str | None = Field(default=None, min_length=1, max_length=100)
    origin_name: str | None = Field(default=None, min_length=1, max_length=100)
    max_minutes: int | None = Field(default=None, ge=1, le=180, strict=True)


class Interpretation(StrictModel):
    request_type: RequestType
    changes: Changes


class Place(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    x: float = Field(ge=124, le=132)
    y: float = Field(ge=33, le=39)
    address: str = Field(default="", max_length=500)
    place_url: HttpUrl | None = None
    road_address: str | None = None
    phone: str | None = None
    category_code: str | None = None
    category_name: str | None = None
    collected_at: datetime | None = None

    @field_validator("place_url", mode="before")
    @classmethod
    def optional_place_url(cls, value):
        # A missing or malformed optional link must not discard a valid place.
        if not value:
            return None
        try:
            return HttpUrl(value)
        except ValueError:
            return None


class TrafficEvidence(StrictModel):
    status: Grade = "확인 불가"
    scope: str = "도착지 주변 도로"
    road_ids: list[str] = Field(default_factory=list)
    source_time: datetime | None = None
    radius_m: int = 500
    source: str = "seoul_citydata"


class ParkingEvidence(StrictModel):
    id: str
    name: str
    distance_m: float = Field(ge=0)
    source: str
    source_time: datetime | None = None


class Candidate(StrictModel):
    place: Place
    duration_seconds: int = Field(ge=0, strict=True)
    traffic: TrafficEvidence = Field(default_factory=TrafficEvidence)
    parking: ParkingEvidence | None = None
    explanation: str = ""


class SearchState(StrictModel):
    version: Literal[1] = 1
    conditions: Conditions
    next_page: int = Field(default=1, ge=1, le=46)
    is_end: bool = False
    remaining: list[Place] = Field(default_factory=list, max_length=675)
    shown_place_ids: list[str] = Field(default_factory=list, max_length=675)
    searched_place_ids: list[str] = Field(default_factory=list, max_length=675)
    expires_at: int


class ChatRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
        json_schema_extra={
            "example": {
                "message": "가족끼리 먹을 한식집 추천해줘",
                "conditions": {"x": 126.977, "y": 37.5657, "max_minutes": 30},
            }
        },
    )
    message: str = Field(min_length=1, max_length=2000)
    conditions: Conditions | None = None
    state: str | None = Field(default=None, max_length=300000)


class ChatResponse(StrictModel):
    request_type: RequestType
    status: Literal["ok", "exhausted", "search_incomplete", "needs_clarification"]
    conditions: Conditions
    recommendations: list[Candidate] = Field(default_factory=list, max_length=3)
    state: str | None = None
    more_available: bool = False
    message: str = ""
    origin_options: list[Place] = Field(default_factory=list)
    explanation_source: Literal["model_grounded", "template", "none"] = "none"
