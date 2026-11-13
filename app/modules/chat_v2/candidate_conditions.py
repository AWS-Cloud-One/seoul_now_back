"""Exact registered-parking radius facts and candidate predicate evaluation."""

from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.modules.chat_v2.predicates import CandidateAssessment, compare_value, evaluate
from app.repositories.parking_nearby import ParkingNearbyRepository


async def exact_parking(session, coordinates, radius):
    fact = {
        "kind": "registered_parking_exists",
        "radius_m": radius,
        "total_count": None,
        "nearest_distance_meters": None,
        "source": "registered_parking",
        "geometry_version": None,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "unavailable",
    }
    try:
        async with session.begin_nested():
            repository = ParkingNearbyRepository(session)
            version = await repository.get_version()
            if version is None or not version.is_ready or not version.geometry_version:
                return {**fact, "reason_code": "PARKING_DATA_UNAVAILABLE"}
            count, nearest = await repository.summarize_radius(coordinates, radius)
            return {
                **fact,
                "status": "ok",
                "total_count": count,
                "nearest_distance_meters": nearest,
                "geometry_version": version.geometry_version,
            }
    except SQLAlchemyError:
        return {**fact, "status": "error", "reason_code": "PARKING_QUERY_FAILED"}


async def assess(session, candidate, predicate):
    parking, facts = {}, []
    for atom in predicate.atoms() if predicate else []:
        if atom.kind == "registered_parking_exists" and atom.radius_m not in parking:
            parking[atom.radius_m] = await exact_parking(
                session, candidate.place.coordinates, atom.radius_m
            )
            facts.append(parking[atom.radius_m])
        elif atom.kind == "compare":
            value = getattr(candidate, atom.metric)
            facts.append(
                {
                    "kind": "compare",
                    "metric": atom.metric,
                    "value": value,
                    "source": "kakao_mobility",
                    "checked_at": candidate.route_collected_at.isoformat()
                    if candidate.route_collected_at
                    else None,
                }
            )

    def fact(atom):
        if atom.kind == "compare":
            return compare_value(atom.operator, getattr(candidate, atom.metric), atom.value)
        row = parking[atom.radius_m]
        return "unknown" if row["status"] != "ok" else "true" if row["total_count"] > 0 else "false"

    truth = evaluate(predicate, fact)
    return CandidateAssessment(
        place_id=candidate.place.place_id,
        truth=truth,
        reason_code="VERIFIED"
        if truth == "true"
        else "CRITERIA_NOT_MET"
        if truth == "false"
        else "CRITERIA_UNKNOWN",
        facts=facts,
    )
