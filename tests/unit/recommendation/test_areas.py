import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from app.modules.recommendation.areas import SearchArea, select_areas
from tests.unit.recommendation.test_progress import candidate, ids, request, setup


def area(key, grade="원활", minutes=0, parking=1):
    now = datetime.now(UTC)
    return SearchArea(
        area_id=key,
        name=key,
        center={"latitude": 37.57, "longitude": 126.98},
        rings=[[(126.97, 37.56), (126.99, 37.56), (126.99, 37.58), (126.97, 37.58)]],
        source="provided-by-part1",
        geometry_version="v1",
        congestion=grade,
        road_observed_at=now - timedelta(minutes=minutes),
        registered_parking_count=parking,
        parking_checked_at=now,
    )


def test_stale_and_missing_roads_not_treated_as_free_flow():
    origin = request().current_conditions.origin.coordinates
    ranked, warnings = select_areas(
        [area("stale", minutes=31), area("known", "정체"), area("unknown", "확인 불가")], origin
    )
    assert ranked[0].area_id == "known"
    assert warnings[0]["code"] == "SEARCH_AREA_DATA_INCOMPLETE"
    ranked, _ = select_areas([area("none", parking=0), area("parking")], origin)
    assert ranked[0].area_id == "parking"


def test_boundary_filter_and_next_area_without_implementing_lookup():
    async def run():
        service, source, engine = setup()
        engine.area_provider = AsyncMock()
        engine.area_provider.areas.return_value = [area("a"), area("b")]
        outside = candidate("outside")
        outside.place.coordinates.latitude = 37.7
        source.search.side_effect = [([outside], True), ([candidate("valid")], True)]
        outcome = await service.execute(request())
        assert ids(outcome) == ["valid"]
        assert source.search.await_count == 2
        assert source.route.await_count == 1
        assert len(outcome.response.result["search_scope"]["areas"]) == 2

    asyncio.run(run())


def test_polygon_hole_and_empty_coverage():
    selected = area("hole")
    selected.rings.append(
        [(126.975, 37.565), (126.985, 37.565), (126.985, 37.575), (126.975, 37.575)]
    )
    assert not selected.contains(candidate(1).place.coordinates)
    assert select_areas([], request().current_conditions.origin.coordinates)[1]
