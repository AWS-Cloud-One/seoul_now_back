"""Validate shared result facts and retain them in explicit historical rows."""

import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.models import (
    RecommendationCard,
    RecommendationEvidence,
    RecommendationResult,
    RecommendationSearchArea,
)
from app.repositories.recommendation_context import RecommendationSnapshot
from app.schemas.pending import RouteValue
from app.schemas.recommendation_result import (
    ContextCard,
    ContextResult,
    ParkingDistance,
    validate_ranking_decision,
)
from app.schemas.recommendations import RecommendationResponse


def _decimal(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _information(info, prefix):
    return {
        f"{prefix}_{field}": getattr(info, field)
        for field in ("freshness", "observed_at", "collected_at", "source", "reason_code")
    }


def _validate_card(card: ContextCard, position: int, kind: str) -> None:
    place, route, parking, reason = card.place, card.route, card.parking, card.reason
    if place.status != "available" or place.value is None:
        raise ValueError("A card requires an identified place")
    if not re.fullmatch(r"kakao:[^\s]+", place.value.place_id) or not place.value.name.strip():
        raise ValueError("Invalid place identity")
    if not reason.text.strip():
        raise ValueError("Missing reason text")
    if parking.status == "available":
        if (
            parking.search_radius_meters is None
            or not parking.total_count
            or (
                parking.nearest_distance_meters is None
                or parking.nearest_distance_meters > parking.search_radius_meters
            )
        ):
            raise ValueError("Available parking requires a radius, count and nearest distance")
    elif parking.nearest_distance_meters is not None or parking.total_count not in (None, 0):
        raise ValueError("Unknown parking cannot carry confirmed values")

    if kind == "general_candidates":
        if (
            card.rank is not None
            or route.value is not None
            or route.status not in {"unavailable", "error"}
            or reason.source != "none"
            or reason.evidence
            or card.ranking is not None
        ):
            raise ValueError("General candidates cannot claim rank, ETA or recommendation evidence")
        return
    if card.rank != position + 1 or route.status != "available" or route.value is None:
        raise ValueError("Recommended cards require consecutive ranks and confirmed routes")
    if reason.source not in {"ai", "fallback"}:
        raise ValueError("Recommended cards require an explanation source")
    kinds = [item.type for item in reason.evidence]
    if len(kinds) != len(set(kinds)) or "eta" not in kinds:
        raise ValueError("Each recommendation requires ETA evidence without duplicate facts")
    for item in reason.evidence:
        info = item.information
        if item.type == "traffic":
            # The shared type preserves unavailable traffic separately from confirmed values.
            continue
        if info.status != "available" or info.value is None or not item.text.strip():
            raise ValueError("Only confirmed evidence can be stored")
        if item.type == "eta":
            if not isinstance(info.value, RouteValue) or info.model_dump() != route.model_dump():
                raise ValueError("ETA evidence must match the route, including its provenance")
        elif (
            not isinstance(info.value, ParkingDistance)
            or parking.status != "available"
            or info.value.distance_meters != parking.nearest_distance_meters
        ):
            raise ValueError("Parking evidence must match the confirmed nearest distance")


def _validate_ranking(card, payload):
    traffic = next((e.information for e in card.reason.evidence if e.type == "traffic"), None)
    validate_ranking_decision(
        card.ranking,
        payload.ranking_policy,
        traffic=traffic,
        duration_seconds=card.route.value.duration_seconds,
        parking_status=card.parking.status,
        parking_distance=card.parking.nearest_distance_meters,
        created_at=payload.created_at,
    )


def _validate_order(payload):
    if payload.ranking_policy is None:
        return
    groups, keys = {}, []
    grades = {"원활": 0, "서행": 1, "정체": 2}
    for card in payload.cards:
        decision = card.ranking
        if decision is None:
            raise ValueError("Ranked cards require retained comparison decisions")
        current = (decision.traffic_applied, decision.evaluated_at)
        if groups.setdefault(decision.eta_bucket, current) != current:
            raise ValueError("A time bucket must use one comparison mode")
        keys.append(
            (
                decision.eta_bucket,
                grades[decision.adjusted_traffic_grade] if decision.traffic_applied else 0,
                card.route.value.duration_seconds,
                card.place.value.place_id,
            )
        )
    if keys != sorted(keys):
        raise ValueError("Card order must agree with the retained ranking decisions")


def prepare_snapshot(
    response: RecommendationResponse,
    request_id: UUID,
    *,
    token_digest: str,
    expires_at: datetime,
    source_result_id: UUID | None = None,
) -> RecommendationSnapshot:
    """Validate a server-produced result; never infer missing facts or source timestamps."""
    if response.kind not in {"recommendation", "general_candidates"}:
        raise ValueError("Only recommendation results can be registered")
    payload = ContextResult.model_validate(response.result)
    if payload.context_token is not None or payload.context_expires_at is not None:
        raise ValueError("An already issued result must be replayed, never registered again")
    if (
        not response.condition_update.applied
        or payload.conditions != response.condition_update.conditions
    ):
        raise ValueError("Result conditions must match the accepted condition version")
    if payload.conditions.origin is None or not payload.conditions.purposes:
        raise ValueError("Result conditions require an origin and a purpose")
    if response.kind == "recommendation":
        if response.stages.routes not in {"ok", "partial"} or response.stages.explanation not in {
            "ok",
            "fallback",
        }:
            raise ValueError("Invalid recommendation stages")
    elif response.stages.routes != "failed" or response.stages.explanation != "skipped":
        raise ValueError("Invalid general candidate stages")
    if expires_at <= payload.created_at:
        raise ValueError("Context expiry must follow the original result time")

    result = RecommendationResult(
        result_id=payload.result_id,
        trip_id=response.trip_id,
        request_id=request_id,
        condition_version=response.condition_update.version,
        kind=response.kind,
        source_result_id=source_result_id,
        routes_status=response.stages.routes,
        explanation_status=response.stages.explanation,
        created_at=payload.created_at,
        context_token_digest=token_digest,
        context_expires_at=expires_at,
        ranking_policy=payload.ranking_policy.name if payload.ranking_policy else None,
        time_bucket_seconds=payload.ranking_policy.time_bucket_seconds
        if payload.ranking_policy
        else None,
        parking_bonus_radius_m=payload.ranking_policy.parking_bonus_radius_m
        if payload.ranking_policy
        else None,
        traffic_max_age_seconds=payload.ranking_policy.traffic_max_age_seconds
        if payload.ranking_policy
        else None,
        search_fallback=payload.search_scope.fallback if payload.search_scope else None,
        search_more_available=payload.search_scope.more_available if payload.search_scope else None,
    )
    cards, evidence, seen = [], [], set()
    for position, card in enumerate(payload.cards):
        _validate_card(card, position, response.kind)
        if response.kind == "recommendation":
            _validate_ranking(card, payload)
        place = card.place.value
        if place.place_id in seen:
            raise ValueError("A result cannot repeat a place")
        seen.add(place.place_id)
        route, parking = card.route, card.parking
        if (
            route.value is not None
            and payload.conditions.max_travel_seconds is not None
            and route.value.duration_seconds > payload.conditions.max_travel_seconds
        ):
            raise ValueError("A recommended route exceeds the accepted travel limit")
        coordinates = place.coordinates
        cards.append(
            RecommendationCard(
                result_id=result.result_id,
                result_kind=result.kind,
                place_id=place.place_id,
                position=position,
                rank=card.rank,
                place_name=place.name,
                purpose=place.purpose,
                category_code=place.category_code,
                category_name=place.category_name,
                place_location=f"SRID=4326;POINT({coordinates.longitude} {coordinates.latitude})",
                address=place.address,
                road_address=place.road_address,
                phone=place.phone,
                provider_url=place.provider_url,
                **_information(card.place, "place"),
                duration_seconds=route.value.duration_seconds if route.value else None,
                distance_meters=_decimal(route.value.distance_meters) if route.value else None,
                route_status=route.status,
                **_information(route, "route"),
                parking_status=parking.status,
                parking_reason_code=parking.reason_code,
                parking_checked_at=parking.checked_at,
                parking_search_radius_meters=parking.search_radius_meters,
                parking_nearest_distance_meters=_decimal(parking.nearest_distance_meters),
                parking_total_count=parking.total_count,
                reason_text=card.reason.text,
                reason_source=card.reason.source,
                ranking_evaluated_at=card.ranking.evaluated_at if card.ranking else None,
                ranking_eta_bucket=card.ranking.eta_bucket if card.ranking else None,
                ranking_traffic_applied=card.ranking.traffic_applied if card.ranking else None,
                ranking_parking_adjustment=card.ranking.parking_adjustment
                if card.ranking
                else None,
                ranking_adjusted_grade=card.ranking.adjusted_traffic_grade
                if card.ranking
                else None,
            )
        )
        for index, item in enumerate(card.reason.evidence):
            info = item.information
            evidence.append(
                RecommendationEvidence(
                    result_id=result.result_id,
                    result_kind=result.kind,
                    place_id=place.place_id,
                    position=index,
                    type=item.type,
                    text=item.text,
                    status=info.status,
                    duration_seconds=info.value.duration_seconds if item.type == "eta" else None,
                    distance_meters=_decimal(info.value.distance_meters)
                    if item.type in {"eta", "parking"}
                    else None,
                    traffic_grade=info.value.grade
                    if item.type == "traffic" and info.value is not None
                    else None,
                    freshness=info.freshness,
                    observed_at=info.observed_at,
                    collected_at=info.collected_at,
                    source=info.source,
                    reason_code=info.reason_code,
                )
            )
    if response.kind == "recommendation":
        _validate_order(payload)
    areas = []
    for position, area in enumerate(payload.search_scope.areas if payload.search_scope else []):
        areas.append(
            RecommendationSearchArea(
                result_id=result.result_id,
                position=position,
                **area.model_dump(exclude={"center"}),
                center=f"SRID=4326;POINT({area.center.longitude} {area.center.latitude})",
            )
        )
    return RecommendationSnapshot(
        result, payload.conditions, tuple(cards), tuple(evidence), tuple(areas)
    )
