"""Bounded three-valued conditions. Facts and decisions are server-owned."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.common import ContractModel
from app.schemas.recommendations import Purpose

Truth = Literal["true", "false", "unknown"]


class PredicateAtom(ContractModel):
    kind: Literal["registered_parking_exists", "compare", "candidate_outcome", "task_failed"]
    radius_m: int | None = Field(default=None, strict=True, ge=1, le=1000)
    metric: Literal["duration_seconds", "distance_meters"] | None = None
    operator: Literal["lt", "lte", "eq", "gte", "gt"] | None = None
    value: int | None = Field(default=None, strict=True, ge=0, le=200000)
    outcome: Literal["has_verified", "no_matches", "insufficient_evidence"] | None = None

    @model_validator(mode="after")
    def arguments(self):
        required = {
            "registered_parking_exists": {"radius_m"},
            "compare": {"metric", "operator", "value"},
            "candidate_outcome": {"outcome"},
            "task_failed": set(),
        }[self.kind]
        supplied = {
            name
            for name in ("radius_m", "metric", "operator", "value", "outcome")
            if getattr(self, name) is not None
        }
        if required != supplied:
            raise ValueError("Predicate arguments do not match their kind")
        return self


class PredicateGroup(ContractModel):
    kind: Literal["group"] = "group"
    operator: Literal["all", "any"]
    terms: list[PredicateAtom] = Field(min_length=1, max_length=4)


class Predicate(ContractModel):
    operator: Literal["all", "any"] = "all"
    terms: list[Annotated[PredicateAtom | PredicateGroup, Field(discriminator="kind")]] = Field(
        min_length=1, max_length=4
    )

    @model_validator(mode="after")
    def bounded(self):
        if len(self.atoms()) > 8:
            raise ValueError("At most eight predicate leaves are supported")
        return self

    def atoms(self):
        return [
            leaf
            for term in self.terms
            for leaf in (term.terms if isinstance(term, PredicateGroup) else [term])
        ]

    def validate_domain(self, domain):
        allowed = (
            {"registered_parking_exists", "compare"}
            if domain == "candidate"
            else {"candidate_outcome", "task_failed"}
        )
        if any(atom.kind not in allowed for atom in self.atoms()):
            raise ValueError("Predicate is not allowed in this execution phase")


def combine(operator, values) -> Truth:
    if operator == "all":
        return "false" if "false" in values else "unknown" if "unknown" in values else "true"
    return "true" if "true" in values else "unknown" if "unknown" in values else "false"


def evaluate(predicate, fact) -> Truth:
    if predicate is None:
        return "true"
    return combine(
        predicate.operator,
        [
            combine(term.operator, [fact(atom) for atom in term.terms])
            if isinstance(term, PredicateGroup)
            else fact(term)
            for term in predicate.terms
        ],
    )


def compare_value(operator, actual, expected) -> Truth:
    if actual is None:
        return "unknown"
    return (
        "true"
        if {
            "lt": actual < expected,
            "lte": actual <= expected,
            "eq": actual == expected,
            "gte": actual >= expected,
            "gt": actual > expected,
        }[operator]
        else "false"
    )


def outcome_fact(atom, outcome) -> Truth:
    if atom.kind == "task_failed":
        return "true" if outcome == "error" else "false"
    if outcome in {"insufficient_evidence", "error"}:
        if atom.outcome == "insufficient_evidence" and outcome == "insufficient_evidence":
            return "true"
        return "unknown"
    matches = {"has_verified": "recommendation", "no_matches": "no_candidates"}
    return "true" if matches.get(atom.outcome) == outcome else "false"


class Alternative(ContractModel):
    when: Predicate
    omit_parking: bool = False
    max_travel_seconds: int | None = Field(default=None, strict=True, ge=1, le=86400)
    purposes: list[Purpose] | None = Field(default=None, min_length=1, max_length=2)
    evidence_text: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def meaningful(self):
        self.when.validate_domain("outcome")
        if not (self.omit_parking or self.max_travel_seconds or self.purposes):
            raise ValueError("An alternative must explicitly change supported criteria")
        return self


class AfterRecommendation(ContractModel):
    position: int = Field(default=1, strict=True, ge=1, le=3)
    topics: list[
        Literal["place_info", "driving", "parking", "recommendation_reason", "traffic"]
    ] = Field(min_length=1, max_length=4)


class RecommendationOptions(ContractModel):
    scope: Literal["persistent", "request_only"] = "persistent"
    constraints: Predicate | None = None
    commit_guard: Predicate | None = None
    alternative: Alternative | None = None
    after: AfterRecommendation | None = None

    @model_validator(mode="after")
    def domains(self):
        if self.constraints:
            self.constraints.validate_domain("candidate")
        if self.commit_guard:
            self.commit_guard.validate_domain("outcome")
        return self


class CandidateAssessment(ContractModel):
    place_id: str
    truth: Truth
    reason_code: str
    facts: list[dict] = Field(default_factory=list, max_length=8)


class EvaluationSummary(ContractModel):
    assessments: list[CandidateAssessment] = Field(default_factory=list, max_length=15)
    branch: Literal["primary", "alternative"] = "primary"
    primary_outcome: str | None = None
    guard: Truth | None = None
