"""Provisional C01/R02/P01/P03/D01 contracts from API_SPEC 0.12.

These types describe transport only; they do not settle token, auth or AI policy.
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.schemas.common import ApiEnvelope, ContractModel, Information
from app.schemas.location import Coordinates
from app.schemas.parking_nearby import ParkingSummary
from app.schemas.recommendations import (
    Clarification,
    RecommendationData,
    RequestContext,
    Stages,
    WireModel,
)


class RecentMessage(WireModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=500)


class ChatTarget(WireModel):
    result_id: str
    place_id: str
    context_token: str


class ChatRequest(RequestContext):
    message: str = Field(min_length=1, max_length=500)
    recent_messages: list[RecentMessage] = Field(default_factory=list, max_length=10)
    pending_clarification: Clarification | None = None
    target: ChatTarget | None = None


class ChatStages(Stages):
    interpretation: Literal["skipped", "ok", "failed"] = "skipped"


class Answer(WireModel):
    topic: Literal["recommendation_reason", "travel_time", "parking", "population", "scope_notice"]
    place_id: str | None
    text: str
    original_result_id: str | None
    original_recommended_at: AwareDatetime | None
    checked_at: AwareDatetime | None
    # The information value differs by topic; no AI-generated facts are added here.
    information: list[Information[JsonValue]]


class ChatData(RecommendationData):
    kind: Literal[
        "clarification",
        "recommendation",
        "general_candidates",
        "no_candidates",
        "failure",
        "answer",
    ]
    stages: ChatStages
    answer: Answer | None = None

    @model_validator(mode="after")
    def matching_answer(self):
        if (self.answer is not None) != (self.kind == "answer"):
            raise ValueError("answer는 답변 종류에서만 필요합니다.")
        if self.kind in {"answer", "clarification"} and self.condition_update.applied:
            raise ValueError("답변·추가 질문은 조건을 적용하지 않습니다.")
        return self


class ChatResponse(ApiEnvelope[ChatData]):
    pass


class RetryRoutesRequest(RequestContext):
    result_id: str
    context_token: str


class TravelTimeRequest(WireModel):
    request_id: UUID
    trip_id: UUID
    condition_version: int = Field(strict=True, ge=0)
    result_id: str
    context_token: str


class Place(ContractModel):
    place_id: str
    name: str
    purpose: Literal["cafe", "food", "walk", "exhibition"] | None
    category_code: str | None
    category_name: str | None
    coordinates: Coordinates
    address: str | None
    road_address: str | None
    phone: str | None
    provider_url: str | None


class PlaceDetailData(ContractModel):
    result_id: str
    place: Information[Place]
    parking: ParkingSummary
    original_recommended_at: AwareDatetime | None
    checked_at: AwareDatetime


class PlaceDetailResponse(ApiEnvelope[PlaceDetailData]):
    pass


class RouteValue(ContractModel):
    duration_seconds: int = Field(strict=True, ge=0)
    distance_meters: float = Field(ge=0)


class TravelTimeData(ContractModel):
    trip_id: UUID
    base_condition_version: int = Field(strict=True, ge=0)
    result_id: str
    place_id: str
    original_recommended_at: AwareDatetime | None
    checked_at: AwareDatetime
    route: Information[RouteValue]


class TravelTimeResponse(ApiEnvelope[TravelTimeData]):
    pass


class CollectionRunSummary(ContractModel):
    run_id: UUID
    # Final status/completeness vocabularies must match the team's DB contract.
    status: str
    completeness: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    received: int = Field(strict=True, ge=0)
    accepted: int = Field(strict=True, ge=0)
    rejected: int = Field(strict=True, ge=0)
    duplicates: int = Field(strict=True, ge=0)
    error_code: str | None


class DataStatusData(ContractModel):
    source: str
    target_key: str
    last_run: CollectionRunSummary | None
    last_completed_run: CollectionRunSummary | None
    last_received_at: AwareDatetime | None


class DataStatusResponse(ApiEnvelope[DataStatusData]):
    pass
