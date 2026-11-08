"""Five-minute ETA buckets with congestion and registered-parking adjustment."""

from dataclasses import dataclass
from itertools import groupby

from app.modules.recommendation.types import Candidate


@dataclass(frozen=True)
class RecommendationPolicy:
    max_pages_per_request: int = 3
    max_search_areas: int = 12
    time_bucket_seconds: int = 300
    parking_bonus_radius_m: int = 500


DEFAULT_POLICY = RecommendationPolicy()
GRADES = {"원활": 0, "서행": 1, "정체": 2}


def rank_candidates(
    candidates: list[Candidate], policy: RecommendationPolicy = DEFAULT_POLICY
) -> list[Candidate]:
    if policy.time_bucket_seconds <= 0 or policy.parking_bonus_radius_m < 0:
        raise ValueError("Invalid ranking policy")
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

    ranked = []
    for _, group in groupby(sorted(candidates, key=bucket_key), key=bucket_key):
        bucket = list(group)
        # Fall back for the entire bucket to avoid inconsistent pairwise comparisons.
        known = all(c.congestion in GRADES for c in bucket)
        bucket.sort(
            key=lambda c: (adjusted_grade(c) if known else 0, c.duration_seconds, c.place.place_id)
        )
        ranked.extend(bucket)
    return ranked
