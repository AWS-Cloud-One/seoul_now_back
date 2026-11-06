"""Deterministic ETA bands avoid non-transitive pairwise comparisons."""

from dataclasses import dataclass
from itertools import groupby

from app.schemas.recommendation import Candidate

GRADES = {"원활": 0, "서행": 1, "정체": 2}


@dataclass(frozen=True)
class RecommendationPolicy:
    similar_eta_seconds: int = 180
    road_radius_m: int = 500
    road_max_age_seconds: int = 900
    max_pages_per_request: int = 3


DEFAULT_POLICY = RecommendationPolicy()


def rank_candidates(
    candidates: list[Candidate],
    policy: RecommendationPolicy = DEFAULT_POLICY,
) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (c.duration_seconds, c.place.id))
    ranked = []
    while ordered:
        anchor = ordered[0].duration_seconds
        end = next(
            (
                i
                for i, c in enumerate(ordered)
                if c.duration_seconds - anchor > policy.similar_eta_seconds
            ),
            len(ordered),
        )
        band, ordered = ordered[:end], ordered[end:]
        if all(c.traffic.status in GRADES for c in band):
            band.sort(key=lambda c: GRADES[c.traffic.status])
            for _, group in groupby(band, key=lambda c: c.traffic.status):
                equal_traffic = list(group)
                if all(c.parking is not None for c in equal_traffic):
                    equal_traffic.sort(
                        key=lambda c: (-c.parking.available_spaces, c.duration_seconds, c.place.id)
                    )
                ranked.extend(equal_traffic)
        else:
            ranked.extend(band)
    return ranked
