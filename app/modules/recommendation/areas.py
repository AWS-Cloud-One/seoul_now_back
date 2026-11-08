"""Part 2 area-selection policy. Spatial/source facts are provided by Part 1."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field

from app.schemas.common import ContractModel
from app.schemas.location import Coordinates


class SearchArea(ContractModel):
    area_id: str
    name: str
    center: Coordinates
    # Polygon rings: longitude/latitude, outer ring followed by optional holes.
    rings: list[list[tuple[float, float]]] = Field(min_length=1)
    source: str
    geometry_version: str
    congestion: Literal["원활", "서행", "정체", "확인 불가"] = "확인 불가"
    road_observed_at: AwareDatetime | None = None
    registered_parking_count: int | None = Field(default=None, ge=0)
    parking_checked_at: AwareDatetime | None = None

    def contains(self, coordinates):
        x, y = coordinates.longitude, coordinates.latitude

        def inside(ring):
            result = False
            for (a, b), (c, d) in zip(ring, ring[1:] + ring[:1], strict=True):
                if min(a, c) <= x <= max(a, c) and min(b, d) <= y <= max(b, d):
                    if abs((x - a) * (d - b) - (y - b) * (c - a)) < 1e-12:
                        return True
                if (b > y) != (d > y) and x < (c - a) * (y - b) / (d - b) + a:
                    result = not result
            return result

        return (
            len(self.rings[0]) >= 3
            and inside(self.rings[0])
            and not any(inside(ring) for ring in self.rings[1:])
        )


def select_areas(areas, origin, *, now=None, max_areas=12):
    """Proposal: valid road grade, registered parking presence, distance, stable ID."""
    now = now or datetime.now(UTC)
    grades = {"원활": 0, "서행": 1, "정체": 2}

    def fresh(at):
        return at is not None and timedelta(0) <= now - at <= timedelta(minutes=30)

    def key(area):
        known = area.congestion in grades and fresh(area.road_observed_at)
        parking = area.registered_parking_count
        has_parking = parking is not None and parking > 0 and fresh(area.parking_checked_at)
        distance = (area.center.latitude - origin.latitude) ** 2 + (
            (area.center.longitude - origin.longitude) * 0.8
        ) ** 2
        return (
            not known,
            grades[area.congestion] if known else 3,
            not has_parking,
            distance,
            area.area_id,
        )

    unique = {area.area_id: area for area in areas}
    ordered = sorted(unique.values(), key=key)
    warnings = []
    if not ordered or any(
        not fresh(a.road_observed_at) or a.congestion not in grades for a in ordered
    ):
        warnings.append(
            {
                "code": "SEARCH_AREA_DATA_INCOMPLETE",
                "message": "도로 자료가 일부 미확인입니다. 서울 전체 탐색 결과가 아닙니다.",
            }
        )
    if len(ordered) > max_areas:
        warnings.append(
            {"code": "SEARCH_AREA_LIMITED", "message": "선정된 일부 지역만 탐색합니다."}
        )
    return ordered[:max_areas], warnings
