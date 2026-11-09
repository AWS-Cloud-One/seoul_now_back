"""Render the current public card contract from verified provider and DB values."""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.parking_nearby import ParkingSummary
from app.schemas.recommendation_result import ContextCard, ContextResult


def unavailable_parking(reason="NO_RECORD", *, error=False):
    return ParkingSummary(
        status="error" if error else "unavailable",
        reason_code=reason,
        checked_at=datetime.now(UTC),
        search_radius_meters=None,
        nearest_distance_meters=None,
        items=[],
        total_count=None,
        next_cursor=None,
    )


def information(value, source, *, collected=None, freshness="unknown", reason=None):
    return dict(
        value=value,
        status="available" if value is not None else "unavailable",
        freshness=freshness,
        observed_at=None,
        collected_at=collected,
        source=source,
        reason_code=reason,
    )


def result(conditions, cards):
    # The execution owner attaches the context token after persistence.
    return ContextResult(
        result_id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        conditions=conditions.model_dump(mode="json"),
        context_token=None,
        context_expires_at=None,
        cards=cards,
    ).model_dump(mode="json", exclude_unset=True)


def card(candidate, rank=None, explanation=None, source="none"):
    routed = candidate.duration_seconds is not None
    route = information(
        {
            "duration_seconds": candidate.duration_seconds,
            "distance_meters": candidate.distance_meters,
        }
        if routed
        else None,
        "kakao_mobility",
        collected=candidate.route_collected_at.isoformat()
        if candidate.route_collected_at
        else None,
        freshness="fresh" if routed else "unknown",
        reason=None if routed else "PROVIDER_ERROR",
    )
    parking = candidate.parking or unavailable_parking()
    evidence = []
    if routed:
        evidence.append(
            dict(
                type="traffic",
                text="도착지 주변 도로: " + candidate.congestion,
                information=information(
                    None
                    if candidate.congestion == "확인 불가"
                    else {"grade": candidate.congestion},
                    candidate.congestion_source,
                    freshness=candidate.congestion_freshness,
                    reason=candidate.congestion_reason_code
                    or ("NO_OBSERVATION" if candidate.congestion == "확인 불가" else None),
                ),
            )
        )
        evidence[-1]["information"]["observed_at"] = (
            candidate.congestion_observed_at.isoformat()
            if candidate.congestion_observed_at
            else None
        )
        evidence.append(
            dict(
                type="eta",
                text=f"자동차 이동시간 {candidate.duration_seconds}초",
                information=route,
            )
        )
        if parking.nearest_distance_meters is not None:
            evidence.append(
                dict(
                    type="parking",
                    text=f"등록 주차장까지 직선거리 {parking.nearest_distance_meters:g}m",
                    information=information(
                        {"distance_meters": parking.nearest_distance_meters},
                        # Computed across registered facilities; original sources are in items.
                        None,
                        collected=None,
                    ),
                )
            )
    return ContextCard(
        rank=rank,
        place=information(
            candidate.place.model_dump(mode="json"),
            "kakao_local",
            collected=candidate.collected_at.isoformat() if candidate.collected_at else None,
        ),
        route=route,
        parking=parking.model_dump(mode="json"),
        reason=dict(
            text=explanation or "자동차 이동시간과 순위는 확인되지 않았습니다.",
            source=source,
            evidence=evidence,
        ),
        ranking=candidate.ranking if routed else None,
    ).model_dump(mode="json")
