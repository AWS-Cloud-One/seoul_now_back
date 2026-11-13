"""First F02 slice: message -> interpretation proposal -> validated headcount plan.

These types check data consistency, not authentication or natural-language truth.
Only the server validator may turn a model proposal into a ValidatedPlan.
No model, repository, FastAPI dependency or settings are loaded here.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.modules.chat_v2.dialogue_context import (
    ComparisonMetric,
    DestinationArea,
    PendingRead,
    PlaceReference,
    RecommendationDraft,
    RecommendationFlow,
    Topic,
)
from app.modules.chat_v2.predicates import RecommendationOptions
from app.schemas.common import ContractModel
from app.schemas.recommendations import Conditions, PositiveInt, Purpose

Nonblank = Annotated[str, Field(min_length=1, pattern=r"\S")]
Version = Annotated[int, Field(strict=True, ge=0)]
Scope = Literal["persistent", "request_only"]
HeadcountFields = Annotated[list[Literal["headcount"]], Field(max_length=1)]


class MessageInput(ContractModel):
    type: Literal["message"] = "message"
    text: Nonblank = Field(max_length=2000)
    reply_to_clarification_id: Nonblank | None = None


class ViewedContext(ContractModel):
    result_id: UUID | None = None
    clarification_id: Nonblank | None = None
    collection_id: UUID | None = None
    place_ref: Nonblank | None = None


class RetryInput(ContractModel):
    type: Literal["retry"]
    source_run_id: UUID
    task_ids: list[Literal["interpretation", "recommendation", "lookup", "search", "action"]] = (
        Field(min_length=1, max_length=1)
    )


class ActionInput(ContractModel):
    type: Literal["action"]
    action_id: UUID


class RunRequest(ContractModel):
    """Client submits text, a server-issued choice, or an explicit retry."""

    schema_version: Literal[2] = 2
    request_id: UUID
    expected_condition_version: Version
    expected_state_version: Version
    input: Annotated[MessageInput | RetryInput | ActionInput, Field(discriminator="type")]
    viewed_context: ViewedContext | None = None

    @model_validator(mode="after")
    def action_context_is_server_owned(self):
        if self.input.type == "action" and self.viewed_context is not None:
            raise ValueError("Action targets are supplied by the server")
        return self

    @field_validator("input", mode="before")
    @classmethod
    def message_default(cls, value):
        if isinstance(value, dict) and "type" not in value:
            return {"type": "message", **value}
        return value


class RunIdentity(ContractModel):
    """Allocated and ownership-checked by the server, never by the model."""

    conversation_id: UUID
    trip_id: UUID
    run_id: UUID
    request_id: UUID


class PendingHeadcountQuestion(ContractModel):
    clarification_id: Nonblank
    scope: Scope
    question: Nonblank
    field: Literal["headcount", "origin", "purposes", "search_area", "target", "lookup_origin"] = (
        "headcount"
    )


class ConversationContext(ContractModel):
    identity: RunIdentity
    condition_version: Version
    state_version: Version
    current_conditions: Conditions
    search_area: DestinationArea | None = None
    pending_question: PendingHeadcountQuestion | None = None
    draft: RecommendationDraft | None = None
    references: list[PlaceReference] = Field(default_factory=list, max_length=35)
    focus_ref: str | None = None
    pending_read: PendingRead | None = None
    active_flow: RecommendationFlow | None = None


class HeadcountChangeIntent(ContractModel):
    kind: Literal["change_headcount"]
    headcount: PositiveInt
    scope: Scope
    evidence_text: Nonblank


class NeedsInputIntent(ContractModel):
    kind: Literal["needs_input"]
    question: Nonblank
    required_field: Literal[
        "headcount", "origin", "purposes", "search_area", "lookup_origin", "target"
    ]


class UnsupportedIntent(ContractModel):
    kind: Literal["unsupported"]
    reason: Nonblank


class InterpretationProposal(ContractModel):
    """An untrusted interpretation, with no run IDs, versions or success flags.

    The first slice covers one headcount change, a question, or an unsupported request.
    The full multi-operation proposal in the design is deliberately not implemented.
    """

    schema_version: Literal[1] = 1
    decision: Annotated[
        HeadcountChangeIntent | NeedsInputIntent | UnsupportedIntent,
        Field(discriminator="kind"),
    ]


class ValidatedPlan(ContractModel):
    """Server-owned F02 plan. Values outside headcount must remain unchanged.

    Constructing this model does not verify evidence, ownership, current DB versions
    or geography. Those checks belong to the PlanValidator component.
    """

    phase: Literal["validated"] = "validated"
    feature_id: Literal["F02"] = "F02"
    context: ConversationContext
    scope: Literal["persistent"] = "persistent"
    requested_headcount: PositiveInt
    effective_conditions: Conditions
    changed_fields: HeadcountFields

    @model_validator(mode="after")
    def headcount_only(self):
        before = self.context.current_conditions.model_dump()
        after = self.effective_conditions.model_dump()
        actual_changes = [key for key in before if before[key] != after[key]]
        if actual_changes != self.changed_fields:
            raise ValueError("Only the declared headcount change may enter the F02 plan")
        if self.effective_conditions.headcount != self.requested_headcount:
            raise ValueError("The planned headcount must match the approved value")
        origin = self.effective_conditions.origin
        if origin is None or not origin.supported or not self.effective_conditions.purposes:
            raise ValueError("Recommendation preparation requires a confirmed origin and purpose")
        return self

    @property
    def requires_recommendation(self) -> bool:
        """An unchanged value alone does not request another recommendation."""
        return bool(self.changed_fields)


class RecommendationIntent(RecommendationOptions):
    kind: Literal["recommend"]
    origin_query: Nonblank | None = Field(default=None, max_length=100)
    search_area_query: Nonblank | None = Field(default=None, max_length=100)
    search_radius_m: int | None = Field(default=None, strict=True)
    spatial_relation: Literal["area", "near", "inside"] | None = None
    purposes: list[Purpose] | None = Field(default=None, min_length=1, max_length=2)
    headcount: PositiveInt | None = None
    max_travel_seconds: PositiveInt | None = None
    scope: Scope = "persistent"
    use_previous: bool = False
    evidence_text: Nonblank


class OriginSelectionIntent(ContractModel):
    kind: Literal["select_origin"]
    for_lookup: bool = False
    position: int = Field(strict=True, ge=1, le=5)
    evidence_text: Nonblank


class DestinationSelectionIntent(ContractModel):
    kind: Literal["select_search_area"]
    position: int = Field(strict=True, ge=1, le=3)
    evidence_text: Nonblank


class PlaceSearchIntent(ContractModel):
    kind: Literal["search_places"]
    query: Nonblank = Field(max_length=100)
    evidence_text: Nonblank


class TargetSelector(ContractModel):
    kind: Literal["position", "reference", "focus", "name"]
    position: int | None = Field(default=None, strict=True, ge=1, le=5)
    ref_id: Nonblank | None = None
    name: Nonblank | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def exactly_one(self):
        expected = {"position": "position", "reference": "ref_id", "name": "name"}.get(self.kind)
        if {k for k in ("position", "ref_id", "name") if getattr(self, k) is not None} != (
            {expected} if expected else set()
        ):
            raise ValueError("Target arguments must match its selector")
        return self


class ReadIntent(ContractModel):
    kind: Literal["read"]
    origin_query: Nonblank | None = Field(default=None, max_length=100)
    topics: list[Topic] = Field(min_length=1, max_length=4)
    targets: list[TargetSelector] = Field(min_length=1, max_length=3)
    evidence_text: Nonblank

    @model_validator(mode="after")
    def origin_only_for_driving(self):
        if self.origin_query and "driving" not in self.topics:
            raise ValueError("A lookup origin is only meaningful for driving")
        return self


class CompareIntent(ContractModel):
    kind: Literal["compare"]
    metric: ComparisonMetric = "duration_seconds"
    targets: list[TargetSelector] = Field(min_length=2, max_length=3)
    origin_query: Nonblank | None = Field(default=None, max_length=100)
    include_traffic: bool = False
    evidence_text: Nonblank


class DialogueProposal(ContractModel):
    schema_version: Literal[2] = 2
    decision: Annotated[
        HeadcountChangeIntent
        | NeedsInputIntent
        | UnsupportedIntent
        | RecommendationIntent
        | OriginSelectionIntent
        | DestinationSelectionIntent
        | PlaceSearchIntent
        | ReadIntent
        | CompareIntent,
        # Comparison is a bounded server calculation over fresh, common-origin reads.
        Field(discriminator="kind"),
    ]


ConditionFields = Annotated[
    list[Literal["origin", "purposes", "headcount", "max_travel_seconds"]], Field(max_length=4)
]


class RecommendationPlan(RecommendationOptions):
    phase: Literal["validated"] = "validated"
    feature_id: Literal["F01"] = "F01"
    search_area: DestinationArea | None = None
    context: ConversationContext
    scope: Scope = "persistent"
    flow_id: UUID | None = None
    flow_excluded: list[str] = Field(default_factory=list)
    effective_conditions: Conditions
    changed_fields: ConditionFields

    @model_validator(mode="after")
    def matching_changes(self):
        before, after = (
            self.context.current_conditions.model_dump(),
            self.persisted_conditions.model_dump(),
        )
        if [key for key in before if before[key] != after[key]] != self.changed_fields:
            raise ValueError("Plan must declare exactly the changed condition fields")
        origin = self.effective_conditions.origin
        if not self.effective_conditions.purposes:
            raise ValueError("A purpose is required")
        if origin is not None and not origin.supported:
            raise ValueError("Only a confirmed supported origin is allowed")
        if self.effective_conditions.max_travel_seconds is not None and origin is None:
            raise ValueError("A travel-time constraint requires an origin")
        if (
            self.constraints
            and any(a.kind == "compare" for a in self.constraints.atoms())
            and origin is None
        ):
            raise ValueError("Driving predicates require a confirmed origin")
        if self.scope == "request_only" and self.flow_id is None:
            raise ValueError("Request-only recommendations need an owned flow identity")
        return self

    @property
    def persisted_conditions(self):
        return (
            self.context.current_conditions
            if self.scope == "request_only"
            else self.effective_conditions
        )

    @property
    def requires_recommendation(self):
        return True
