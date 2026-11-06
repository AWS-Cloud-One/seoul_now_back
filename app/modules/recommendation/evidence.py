"""Select measured nearby evidence; unknown is never inferred as zero."""

import math
from datetime import UTC, datetime

from pydantic import ValidationError

from app.modules.data_pipeline.normalizers.parking import source_time
from app.modules.recommendation.ranking import DEFAULT_POLICY, GRADES
from app.schemas.recommendation import ParkingEvidence, TrafficEvidence


def segment_distance(x, y, a, b):
    # Local equirectangular projection: sufficient for a 500 m Seoul neighbourhood.
    scale_x = 111320 * math.cos(math.radians(y))
    ax, ay = (a[0] - x) * scale_x, (a[1] - y) * 111320
    bx, by = (b[0] - x) * scale_x, (b[1] - y) * 111320
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    t = max(0, min(1, -(ax * dx + ay * dy) / denominator)) if denominator else 0
    return math.hypot(ax + t * dx, ay + t * dy)


def traffic_evidence(x, y, features, now=None, policy=DEFAULT_POLICY):
    now = now or datetime.now(UTC)
    known = []
    for feature in features:
        props = feature.get("properties", {})
        observed = source_time(props.get("source_time"))
        grade = props.get("traffic_status")
        if (
            grade not in GRADES
            or observed is None
            or not 0 <= (now - observed).total_seconds() <= policy.road_max_age_seconds
        ):
            continue
        points = feature.get("geometry", {}).get("coordinates", [])
        try:
            distances = [
                segment_distance(x, y, a, b) for a, b in zip(points, points[1:], strict=False)
            ]
            if not distances or min(distances) > policy.road_radius_m:
                continue
        except ValueError, TypeError, IndexError:
            continue
        known.append((grade, str(feature["id"]), observed))
    if not known:
        return TrafficEvidence(radius_m=policy.road_radius_m)
    worst = max(known, key=lambda row: GRADES[row[0]])[0]
    relevant = [row for row in known if row[0] == worst]
    return TrafficEvidence(
        status=worst,
        road_ids=sorted({r[1] for r in relevant}),
        source_time=min(r[2] for r in relevant),
        radius_m=policy.road_radius_m,
    )


def parking_evidence(results, now=None):
    """Choose the nearest registered record, independent of live availability."""
    confirmed = []
    for result in results:
        for item in result.get("items", []):
            try:
                evidence = ParkingEvidence(
                    id=item["id"],
                    name=item["name"],
                    distance_m=item["distance_m"],
                    source=item["source"],
                    source_time=source_time(item.get("source_updated_at")),
                )
            except KeyError, ValidationError:
                continue
            confirmed.append(evidence)
    return min(confirmed, key=lambda p: (p.distance_m, p.id), default=None)
