"""Legacy ETA/parking snapshots with fixed facts; no external calls.

Current-policy engine round trips are exercised in the result-contract tests.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.models import ChatConditionPurpose, ChatConditionVersion, ChatSession
from app.modules.recommendation.cards import card, result, unavailable_parking
from app.modules.recommendation.types import Candidate
from app.schemas.parking_nearby import ParkingSummary
from app.schemas.pending import Place
from app.schemas.recommendations import Conditions, ConditionUpdate, RecommendationResponse, Stages

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


def response(*, general=False):
    conditions = Conditions(
        origin={
            "label": "서울역",
            "coordinates": {"latitude": 37.55, "longitude": 126.97},
            "address": None,
            "region": "seoul",
            "supported": True,
        },
        purposes=[{"purpose": "cafe", "keyword": "조용한"}],
        group_type="friends",
        age_groups=None,
        headcount=2,
        max_travel_seconds=1800,
    )
    candidate = Candidate(
        place=Place(
            place_id="kakao:original",
            name="당시 카페",
            purpose="cafe",
            category_code="CE7",
            category_name="카페",
            coordinates={"latitude": 37.57, "longitude": 126.98},
            address="당시 주소",
            road_address=None,
            phone=None,
            provider_url=None,
        ),
        collected_at=NOW,
        duration_seconds=None if general else 600,
        distance_meters=None if general else 1234.5,
        route_collected_at=None if general else NOW,
        parking=unavailable_parking()
        if general
        else ParkingSummary(
            status="available",
            reason_code=None,
            checked_at=NOW,
            search_radius_meters=300,
            nearest_distance_meters=95.25,
            items=[],
            total_count=2,
            next_cursor=None,
        ),
    )
    candidate.parking.checked_at = NOW
    payload = result(
        conditions,
        [
            card(
                candidate,
                None if general else 1,
                None if general else "당시 경로와 주차 거리",
                "none" if general else "fallback",
            )
        ],
    )
    payload["created_at"] = NOW.isoformat()
    for item in payload["cards"]:
        item.pop("ranking", None)
        item["reason"]["evidence"] = [
            e for e in item["reason"]["evidence"] if e["type"] != "traffic"
        ]
    return RecommendationResponse(
        trip_id=uuid4(),
        base_condition_version=0,
        kind="general_candidates" if general else "recommendation",
        message="추천 결과",
        condition_update=ConditionUpdate(
            applied=True, conditions=conditions, version=0, changes=[]
        ),
        stages=Stages(
            routes="failed" if general else "ok", explanation="skipped" if general else "fallback"
        ),
        result=payload,
        clarification=None,
        warnings=[],
    )


async def seed_conditions(session, data, *, version=0, new_trip=True):
    if new_trip:
        await session.execute(ChatSession.__table__.insert().values(trip_id=data.trip_id))
    condition = data.condition_update.conditions
    origin = condition.origin
    await session.execute(
        ChatConditionVersion.__table__.insert().values(
            trip_id=data.trip_id,
            version=version,
            origin_label=origin.label,
            origin_location=(
                f"SRID=4326;POINT({origin.coordinates.longitude} {origin.coordinates.latitude})"
            ),
            origin_address=origin.address,
            origin_region=origin.region,
            origin_supported=origin.supported,
            group_type=condition.group_type,
            headcount=condition.headcount,
            max_travel_seconds=condition.max_travel_seconds,
        )
    )
    for position, purpose in enumerate(condition.purposes):
        await session.execute(
            ChatConditionPurpose.__table__.insert().values(
                trip_id=data.trip_id,
                condition_version=version,
                purpose=purpose.purpose,
                keyword=purpose.keyword,
                position=position,
            )
        )
