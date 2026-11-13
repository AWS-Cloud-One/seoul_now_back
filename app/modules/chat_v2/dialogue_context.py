"""Server-owned drafts and bounded references; never accepted as model coordinates."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue

from app.modules.chat_v2.predicates import Predicate, RecommendationOptions
from app.modules.chat_v2.region_contracts import DestinationArea, DestinationOption
from app.schemas.common import ContractModel
from app.schemas.location import Location
from app.schemas.query_services import FoundPlace
from app.schemas.recommendations import Conditions

Topic = Literal["place_info", "driving", "parking", "recommendation_reason", "traffic"]
ComparisonMetric = Literal["duration_seconds", "distance_meters"]


class OriginOption(ContractModel):
    option_id: str
    position: int = Field(strict=True, ge=1, le=5)
    location: Location


class RecommendationDraft(RecommendationOptions):
    base_condition_version: int = Field(strict=True, ge=0)
    conditions: Conditions
    search_area: DestinationArea | None = None
    origin_options: list[OriginOption] = Field(default_factory=list, max_length=5)
    destination_options: list[DestinationOption] = Field(default_factory=list, max_length=3)
    destination_query: str | None = None
    destination_radius_m: int | None = None
    destination_relation: Literal["area", "near", "inside"] = "area"
    flow_id: UUID | None = None
    flow_excluded: list[str] = Field(default_factory=list)


class RecommendationFlow(ContractModel):
    flow_id: UUID
    result_id: UUID
    conditions: Conditions
    search_area: DestinationArea | None = None
    constraints: Predicate | None = None
    excluded: list[str] = Field(default_factory=list)


class PlaceReference(ContractModel):
    ref_id: str
    collection_id: UUID
    result_id: UUID | None = None
    position: int = Field(strict=True, ge=1, le=5)
    place: FoundPlace


class PendingRead(ContractModel):
    comparison_metric: ComparisonMetric | None = None
    target_refs: list[str] = Field(default_factory=list, max_length=3)
    origin_options: list[OriginOption] = Field(default_factory=list, max_length=5)
    clarification_id: str
    topics: list[Topic] = Field(min_length=1, max_length=4)
    question: str


class SourceReference(ContractModel):
    source_id: str
    provider: str
    record_ref: str
    collected_at: AwareDatetime | None
    checked_at: AwareDatetime | None = None


class ReadItem(ContractModel):
    topic: Topic
    place_ref: str
    status: Literal["ok", "empty", "unavailable", "error"]
    data: JsonValue = None
    sources: list[SourceReference] = Field(default_factory=list)
    reason_code: str | None = None
    retryable: bool = False


class LookupResult(ContractModel):
    outcome: Literal["ok", "partial", "empty", "unavailable", "error"]
    items: list[ReadItem] = Field(default_factory=list, max_length=12)
    places: list[PlaceReference] = Field(default_factory=list, max_length=5)
    has_more: bool = False
    reason_code: str | None = None


class ComparisonResult(ContractModel):
    metric: ComparisonMetric
    unit: Literal["seconds", "meters"]
    origin_label: str
    outcome: Literal["complete", "incomplete"]
    values: dict[str, float | None]
    best_refs: list[str] = Field(default_factory=list, max_length=3)
    reason_code: str | None = None


class ContinuationResult(ContractModel):
    status: Literal[
        "pending", "completed", "failed", "blocked", "cancelled", "interrupted", "waiting_input"
    ]
    lookup: LookupResult | None = None
    reason_code: str | None = None
