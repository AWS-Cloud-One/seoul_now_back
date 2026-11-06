"""API_SPEC 0.12: minute bucket, registered parking distance, lexical place ID."""

from dataclasses import dataclass
from itertools import groupby

from app.modules.recommendation.types import Candidate


@dataclass(frozen=True)
class RecommendationPolicy:
    max_pages_per_request: int = 3


DEFAULT_POLICY = RecommendationPolicy()


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (c.duration_seconds // 60, c.place.place_id))
    ranked = []
    for _, group in groupby(ordered, key=lambda c: c.duration_seconds // 60):
        bucket = list(group)
        if all(
            c.parking is not None and c.parking.nearest_distance_meters is not None for c in bucket
        ):
            bucket.sort(key=lambda c: (c.parking.nearest_distance_meters, c.place.place_id))
        ranked.extend(bucket)
    return ranked
