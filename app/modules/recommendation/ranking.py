"""Five-minute ETA buckets with congestion and registered-parking adjustment."""

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby

from app.modules.recommendation.types import Candidate
from app.schemas.recommendation_result import RankingDecision, RankingPolicySnapshot


@dataclass(frozen=True)
class RecommendationPolicy:
    max_pages_per_request: int = 3
    max_search_areas: int = 12
    time_bucket_seconds: int = 300
    parking_bonus_radius_m: int = 500
    traffic_max_age_seconds: int = 1800

    def snapshot(self) -> RankingPolicySnapshot:
        return RankingPolicySnapshot(
            time_bucket_seconds=self.time_bucket_seconds,
            parking_bonus_radius_m=self.parking_bonus_radius_m,
            traffic_max_age_seconds=self.traffic_max_age_seconds,
        )


DEFAULT_POLICY = RecommendationPolicy()
GRADES = {"원활": 0, "서행": 1, "정체": 2}


def rank_candidates(
    candidates: list[Candidate], policy: RecommendationPolicy = DEFAULT_POLICY
) -> list[Candidate]:
    policy.snapshot()
    if any(c.duration_seconds is None for c in candidates):
        raise ValueError("Ranking requires confirmed travel times")

    def bucket_key(c):
        return c.duration_seconds // policy.time_bucket_seconds

    def adjusted_grade(c):
        nearby = (
            c.parking is not None
            and c.parking.status == "available"
            and c.parking.nearest_distance_meters is not None
            and c.parking.nearest_distance_meters <= policy.parking_bonus_radius_m
        )
        # Adjust ordering only, never the actual grade or parking availability.
        return max(0, GRADES[c.congestion] - int(nearby))

    evaluated_at = datetime.now(UTC)
    values = [c.model_copy(deep=True) for c in candidates]
    for c in values:
        age = (
            (evaluated_at - c.congestion_observed_at).total_seconds()
            if c.congestion_observed_at is not None
            else None
        )
        c.congestion_freshness, c.congestion_reason_code = "unknown", "NO_OBSERVATION"
        if age is not None and age > policy.traffic_max_age_seconds:
            c.congestion_freshness, c.congestion_reason_code = "stale", "STALE_DATA"
        elif age is not None and age < 0:
            c.congestion_reason_code = "INVALID_OBSERVATION_TIME"
        elif c.congestion in GRADES and age is not None and (c.congestion_source or "").strip():
            c.congestion_freshness, c.congestion_reason_code = "fresh", None
        if c.congestion_freshness != "fresh":
            c.congestion = "확인 불가"

    ranked = []
    for _, group in groupby(sorted(values, key=bucket_key), key=bucket_key):
        bucket = list(group)
        # Fall back for the entire bucket to avoid inconsistent pairwise comparisons.
        known = all(c.congestion in GRADES for c in bucket)
        for c in bucket:
            adjusted = adjusted_grade(c) if known else None
            c.ranking = RankingDecision(
                evaluated_at=evaluated_at,
                eta_bucket=bucket_key(c),
                traffic_applied=known,
                parking_adjustment=GRADES[c.congestion] - adjusted if known else 0,
                adjusted_traffic_grade=list(GRADES)[adjusted] if known else None,
            )
        bucket.sort(
            key=lambda c: (adjusted_grade(c) if known else 0, c.duration_seconds, c.place.place_id)
        )
        ranked.extend(bucket)
    return ranked
