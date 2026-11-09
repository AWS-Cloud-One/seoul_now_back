"""Shared engine/storage contract for historical facts and ranking decisions."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import ContractModel, Information
from app.schemas.location import Coordinates
from app.schemas.parking_nearby import ParkingSummary
from app.schemas.pending import Place, RouteValue
from app.schemas.recommendations import Conditions

TrafficGrade = Literal["원활", "서행", "정체"]


class RankingPolicySnapshot(ContractModel):
    name: Literal["eta_traffic_parking_v1"] = "eta_traffic_parking_v1"
    time_bucket_seconds: int = Field(strict=True, gt=0)
    parking_bonus_radius_m: int = Field(strict=True, ge=0)
    traffic_max_age_seconds: int = Field(default=1800, strict=True, gt=0)


class RankingDecision(ContractModel):
    evaluated_at: AwareDatetime
    eta_bucket: int = Field(strict=True, ge=0)
    traffic_applied: bool
    parking_adjustment: int = Field(strict=True, ge=0, le=1)
    adjusted_traffic_grade: TrafficGrade | None

    @model_validator(mode="after")
    def consistent_comparison(self):
        if self.traffic_applied != (self.adjusted_traffic_grade is not None):
            raise ValueError("A traffic comparison requires an adjusted grade")
        if not self.traffic_applied and self.parking_adjustment:
            raise ValueError("ETA fallback cannot apply a parking adjustment")
        return self


class SearchAreaSnapshot(ContractModel):
    """Compact selection facts; polygon coordinates stay in the spatial source."""

    area_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    center: Coordinates
    source: str = Field(min_length=1)
    geometry_version: str = Field(min_length=1)
    congestion: Literal["원활", "서행", "정체", "확인 불가"]
    road_observed_at: AwareDatetime | None
    registered_parking_count: int | None = Field(ge=0)
    parking_checked_at: AwareDatetime | None


class SearchScope(ContractModel):
    areas: list[SearchAreaSnapshot]
    fallback: bool
    more_available: bool

    @model_validator(mode="after")
    def consistent_coverage(self):
        if self.fallback != (not self.areas):
            raise ValueError("Origin fallback has no selected areas")
        if len({area.area_id for area in self.areas}) != len(self.areas):
            raise ValueError("Search areas cannot be repeated")
        return self


class ParkingDistance(ContractModel):
    distance_meters: float = Field(ge=0)


class TrafficValue(ContractModel):
    grade: TrafficGrade


class EtaEvidence(ContractModel):
    type: Literal["eta"]
    text: str = Field(min_length=1)
    information: Information[RouteValue]


class ParkingEvidence(ContractModel):
    type: Literal["parking"]
    text: str = Field(min_length=1)
    information: Information[ParkingDistance]


class TrafficEvidence(ContractModel):
    type: Literal["traffic"]
    text: str = Field(min_length=1)
    information: Information[TrafficValue]

    @model_validator(mode="after")
    def consistent_fact(self):
        info = self.information
        if info.status == "available":
            if (
                info.value is None
                or not (info.source or "").strip()
                or info.observed_at is None
                or info.freshness == "stale"
            ):
                raise ValueError("Confirmed traffic requires a grade and current provenance")
        elif info.value is not None or not (info.reason_code or "").strip():
            raise ValueError("Unknown traffic requires an explicit reason and no grade")
        return self


def validate_traffic_time(info, *, at, max_age_seconds):
    if info.status == "available" and not (
        info.observed_at is not None
        and 0 <= (at - info.observed_at).total_seconds() <= max_age_seconds
    ):
        raise ValueError("Traffic was not current at the original comparison time")


def validate_ranking_decision(
    decision, policy, *, traffic, duration_seconds, parking_status, parking_distance, created_at
):
    """Check retained decisions against facts, without recomputing an unseen candidate pool."""
    if traffic is not None:
        validate_traffic_time(
            traffic,
            at=decision.evaluated_at if decision else created_at,
            max_age_seconds=policy.traffic_max_age_seconds if policy else 1800,
        )
    if policy is None and decision is None:
        return
    if policy is None or decision is None or traffic is None:
        raise ValueError("A ranked result requires policy, decision and traffic state")
    if decision.evaluated_at > created_at:
        raise ValueError("Ranking cannot happen after result creation")
    if decision.eta_bucket != duration_seconds // policy.time_bucket_seconds:
        raise ValueError("Ranking bucket must match the original ETA")
    if decision.traffic_applied:
        grades = {"원활": 0, "서행": 1, "정체": 2}
        if traffic.status != "available":
            raise ValueError("Unknown traffic cannot affect the ranking")
        grade = grades[traffic.value.grade]
        nearby = (
            parking_status == "available"
            and parking_distance is not None
            and parking_distance <= policy.parking_bonus_radius_m
        )
        adjustment = int(nearby and grade > 0)
        if (
            decision.parking_adjustment != adjustment
            or grades[decision.adjusted_traffic_grade] != grade - adjustment
        ):
            raise ValueError("Ranking adjustment must match original traffic and parking")


ContextEvidence = Annotated[
    EtaEvidence | ParkingEvidence | TrafficEvidence, Field(discriminator="type")
]


class ContextReason(ContractModel):
    text: str = Field(min_length=1)
    source: Literal["ai", "fallback", "none"]
    evidence: list[ContextEvidence]


class ContextCard(ContractModel):
    rank: int | None = Field(strict=True, ge=1, le=3)
    place: Information[Place]
    route: Information[RouteValue]
    parking: ParkingSummary
    reason: ContextReason
    # Missing on results produced before ranking decisions were retained.
    ranking: RankingDecision | None = None


class ContextResult(ContractModel):
    result_id: UUID
    created_at: AwareDatetime
    conditions: Conditions
    context_token: str | None
    context_expires_at: AwareDatetime | None
    cards: list[ContextCard] = Field(min_length=1, max_length=3)
    ranking_policy: RankingPolicySnapshot | None = None
    # Route retry does not search; legacy results did not retain coverage.
    search_scope: SearchScope | None = None
