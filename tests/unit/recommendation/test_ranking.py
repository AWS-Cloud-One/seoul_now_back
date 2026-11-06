from datetime import UTC, datetime, timedelta

from app.modules.recommendation.evidence import parking_evidence, traffic_evidence
from app.modules.recommendation.ranking import rank_candidates
from app.schemas.recommendation import Candidate, ParkingEvidence, Place, TrafficEvidence


def candidate(identity, seconds, grade=None):
    return Candidate(
        place=Place(id=identity, name=identity, x=126.978, y=37.5665),
        duration_seconds=seconds,
        traffic=TrafficEvidence(status=grade or "확인 불가"),
    )


def parked(identity, seconds, distance, grade="확인 불가"):
    item = candidate(identity, seconds, grade)
    item.parking = ParkingEvidence(
        id="p" + identity,
        name="등록 주차장",
        distance_m=distance,
        source="seoul_getparkinfo",
        source_time=None,
    )
    return item


def ids(items):
    return [c.place.id for c in rank_candidates(items)]


def test_same_minute_uses_parking_distance_not_exact_eta_or_traffic():
    assert ids([parked("a", 600, 200, "원활"), parked("b", 659, 50, "정체")]) == ["b", "a"]


def test_minute_boundary_precedes_parking_and_traffic():
    assert ids([parked("a", 599, 900, "정체"), parked("b", 600, 1, "원활")]) == ["a", "b"]


def test_one_unknown_distance_skips_parking_for_entire_bucket():
    assert ids([parked("2", 600, 1), candidate("10", 659), parked("3", 601, 100)]) == [
        "10",
        "2",
        "3",
    ]


def test_equal_parking_distance_uses_lexical_id_not_seconds():
    assert ids([parked("2", 600, 50), parked("10", 659, 50)]) == ["10", "2"]


def test_traffic_does_not_override_minute_buckets():
    assert ids([candidate("a", 1200, "정체"), candidate("b", 1320, "원활")]) == ["a", "b"]


def test_registered_parking_uses_nearest_without_live_vacancies():
    items = [
        {
            "id": "far",
            "name": "far",
            "distance_m": 300,
            "source": "seoul_citydata_prk",
            "available_spaces": 100,
            "real_time_available": True,
        },
        {
            "id": "near",
            "name": "near",
            "distance_m": 50,
            "source": "seoul_getparkinfo",
            "real_time_available": False,
            "source_updated_at": None,
        },
    ]
    evidence = parking_evidence([{"items": items}])
    assert evidence.id == "near"
    assert evidence.source_time is None
    assert "available_spaces" not in evidence.model_dump()


def test_nearby_road_uses_segment_distance_and_source_freshness():
    now = datetime(2026, 11, 5, tzinfo=UTC)
    feature = {
        "id": "road1",
        "geometry": {"coordinates": [[126.96, 37.5665], [127, 37.5665]]},
        "properties": {
            "traffic_status": "정체",
            "source_time": now.isoformat(),
            "collected_at": now.isoformat(),
        },
    }
    assert traffic_evidence(126.978, 37.5665, [feature], now).status == "정체"
    feature["properties"]["source_time"] = (now - timedelta(minutes=16)).isoformat()
    assert traffic_evidence(126.978, 37.5665, [feature], now).status == "확인 불가"
    feature["properties"]["source_time"] = (now + timedelta(minutes=1)).isoformat()
    assert traffic_evidence(126.978, 37.5665, [feature], now).status == "확인 불가"
