"""API_SPEC 0.12: minute bucket, registered parking distance, lexical place ID."""

from dataclasses import dataclass
from itertools import groupby

from app.schemas.recommendation import Candidate

GRADES = {"원활": 0, "서행": 1, "정체": 2}


@dataclass(frozen=True)
class RecommendationPolicy:
    road_radius_m: int = 500
    road_max_age_seconds: int = 900
    max_pages_per_request: int = 3


DEFAULT_POLICY = RecommendationPolicy()


def rank_candidates(
    candidates: list[Candidate],
    policy: RecommendationPolicy = DEFAULT_POLICY,
) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (c.duration_seconds // 60, c.place.id))
    ranked = []
    for _, group in groupby(ordered, key=lambda c: c.duration_seconds // 60):
        bucket = list(group)
        if all(c.parking is not None for c in bucket):
            bucket.sort(key=lambda c: (c.parking.distance_m, c.place.id))
        ranked.extend(bucket)
    return ranked
