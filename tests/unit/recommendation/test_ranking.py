from app.modules.recommendation.ranking import rank_candidates
from app.schemas.recommendation import Candidate, ParkingEvidence, Place, TrafficEvidence


def candidate(identity, seconds, grade="확인 불가", distance=None):
    return Candidate(
        place=Place(id=identity, name=identity, x=126.978, y=37.5665),
        duration_seconds=seconds,
        traffic=TrafficEvidence(status=grade),
        parking=None
        if distance is None
        else ParkingEvidence(id="p" + identity, name="등록 주차장", distance_m=distance),
    )


def ids(*items):
    return [c.place.id for c in rank_candidates(list(items))]


def test_less_congested_wins_within_five_minutes():
    assert ids(candidate("a", 600, "정체"), candidate("b", 899, "원활")) == ["b", "a"]


def test_five_minute_boundary_precedes_traffic_and_parking():
    assert ids(candidate("a", 599, "정체"), candidate("b", 600, "원활", 1)) == ["a", "b"]


def test_parking_at_500m_offsets_one_grade_and_preserves_evidence():
    a = candidate("a", 610, "서행", 500)
    b = candidate("b", 620, "원활")
    assert ids(b, a) == ["a", "b"]
    assert a.traffic.status == "서행"
    assert ids(candidate("a", 610, "서행", 500.01), b) == ["b", "a"]


def test_parking_cannot_offset_two_grades():
    assert ids(candidate("a", 610, "정체", 10), candidate("b", 620, "원활")) == ["b", "a"]


def test_equal_adjusted_grade_uses_eta_not_parking_distance():
    assert ids(candidate("a", 620, "서행", 10), candidate("b", 610, "서행", 400)) == ["b", "a"]


def test_unknown_grade_falls_back_to_eta_for_entire_bucket():
    assert ids(
        candidate("a", 650, "원활", 1), candidate("b", 620), candidate("c", 610, "정체")
    ) == ["c", "b", "a"]


def test_equal_eta_uses_stable_place_id():
    assert ids(candidate("2", 600), candidate("10", 600)) == ["10", "2"]


def test_parking_does_not_improve_beyond_free_flow():
    assert ids(candidate("a", 620, "원활", 1), candidate("b", 610, "원활")) == ["b", "a"]
