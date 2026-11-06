from datetime import UTC, datetime, timedelta

from app.modules.recommendation.evidence import parking_evidence, traffic_evidence
from app.modules.recommendation.ranking import rank_candidates
from app.schemas.recommendation import Candidate, ParkingEvidence, Place, TrafficEvidence


def test_parking_breaks_equal_traffic_ties_only_when_all_counts_known():
    a, b = candidate("a", 600, "원활"), candidate("b", 660, "원활")
    for item, spaces in ((a, 0), (b, 10)):
        item.parking = ParkingEvidence(
            id=item.place.id,
            name="주차장",
            available_spaces=spaces,
            distance_m=100,
            source="seoul_citydata_prk",
            source_time=datetime.now(UTC),
        )
    assert [c.place.id for c in rank_candidates([a, b])] == ["b", "a"]
    a.parking = None
    assert [c.place.id for c in rank_candidates([a, b])] == ["a", "b"]


def test_parking_preserves_zero_and_rejects_stale_or_unconfirmed_counts():
    now = datetime.now(UTC)
    item = {
        "id": "p",
        "name": "주차장",
        "available_spaces": 0,
        "distance_m": 50,
        "source": "seoul_citydata_prk",
        "real_time_available": True,
        "availability_updated_at": now.isoformat(),
    }
    assert parking_evidence([{"items": [item]}], now).available_spaces == 0
    assert parking_evidence([{"items": [{**item, "real_time_available": False}]}], now) is None
    assert (
        parking_evidence(
            [
                {
                    "items": [
                        {
                            **item,
                            "availability_updated_at": (now - timedelta(minutes=16)).isoformat(),
                        }
                    ]
                }
            ],
            now,
        )
        is None
    )


def test_citydata_parking_preferred_and_municipal_only_used_as_fallback():
    now = datetime.now(UTC)
    city = {
        "id": "city",
        "name": "city",
        "available_spaces": 1,
        "distance_m": 50,
        "source": "seoul_citydata_prk",
        "real_time_available": True,
        "availability_updated_at": now.isoformat(),
    }
    municipal = {
        **city,
        "id": "municipal",
        "source": "seoul_municipal_parking",
        "available_spaces": 50,
    }
    assert parking_evidence([{"items": [municipal]}, {"items": [city]}], now).id == "city"
    assert parking_evidence([{"items": [municipal]}], now).id == "municipal"


def candidate(identity, seconds, grade=None):
    return Candidate(
        place=Place(id=identity, name=identity, x=126.978, y=37.5665),
        duration_seconds=seconds,
        traffic=TrafficEvidence(status=grade or "확인 불가"),
    )


def test_car_reorders_close_eta_by_traffic_but_not_distant_eta():
    items = [
        candidate("a", 1200, "정체"),
        candidate("b", 1320, "원활"),
        candidate("c", 1500, "원활"),
    ]
    assert [c.place.id for c in rank_candidates(items)] == ["b", "a", "c"]


def test_missing_traffic_falls_back_to_eta():
    items = [candidate("a", 1200, "정체"), candidate("b", 1320)]
    assert [c.place.id for c in rank_candidates(items)] == ["a", "b"]


def test_anchor_prevents_chained_three_minute_groups():
    items = [candidate("a", 600, "정체"), candidate("b", 780, "서행"), candidate("c", 960, "원활")]
    assert [c.place.id for c in rank_candidates(items)] == ["b", "a", "c"]


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
