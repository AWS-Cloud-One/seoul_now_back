"""PostGIS checks of corresponding road segments, beyond minimum point distance."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.modules.data_pipeline.normalizers.road_geometry import MATCH_POLICY, classify_match
from app.repositories.spatial import SpatialRepository

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "service,standard,status,extent,order",
    [
        ([(0, 0), (1000, 0)], [(200, 5), (400, 5)], "matched", "standard_within_service", "same"),
        (
            [(0, 0), (1000, 0)],
            [(400, 5), (200, 5)],
            "matched",
            "standard_within_service",
            "reversed",
        ),
        ([(200, 5), (400, 5)], [(0, 0), (1000, 0)], "matched", "service_within_standard", "same"),
        (
            [(0, 0), (1000, 0)],
            [(500, -100), (500, 100)],
            "crossing_or_touch_only",
            "standard_within_service",
            None,
        ),
        (
            [(0, 0), (1000, 0)],
            [(500, -20), (500, 20)],
            "crossing_or_touch_only",
            "standard_within_service",
            None,
        ),
        ([(0, 0), (1000, 0)], [(500, 0), (1500, 0)], "partial_overlap", None, None),
        ([(0, 0), (1000, 0)], [(200, 0), (300, 150), (400, 0)], "partial_overlap", None, None),
        (
            [(0, 0), (1000, 0)],
            [(200, 0), (400, 0), (450, 40), (500, 0), (700, 0)],
            "shape_mismatch",
            None,
            None,
        ),
        (
            [(0, 0), (1000, 0)],
            [(200, 0), (400, 200), (200, 200), (400, 0)],
            "ambiguous_curve",
            None,
            None,
        ),
        ([(0, 0), (1000, 0)], [(0, 200), (1000, 200)], "separated", None, None),
        ([(0, 0), (1000, 0)], [(2000, 0), (2200, 0)], "separated", None, None),
        ([(0, 0), (1000, 0)], [(200, 0), (200.5, 0)], "insufficient_extent", None, None),
    ],
)
def test_segment_shape_and_containment(
    normalized_database, service, standard, status, extent, order
):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with async_sessionmaker(engine)() as s:
                repo = SpatialRepository(s)

                async def geometry(points):
                    return await repo.transform_geojson(
                        {
                            "type": "LineString",
                            "coordinates": [[200000 + x, 450000 + y] for x, y in points],
                        },
                        5181,
                    )

                result = classify_match(
                    await repo.line_match_metrics(
                        await geometry(service),
                        await geometry(standard),
                        tolerance_m=MATCH_POLICY["tolerance_m"],
                        segment_length_m=MATCH_POLICY["segment_length_m"],
                    )
                )
                assert result["status"] == status, result
                if extent:
                    assert result["extent"] == extent
                if order:
                    assert result["coordinate_order"] == order
                if status in {"crossing_or_touch_only", "partial_overlap"}:
                    assert result["minimum_distance_m"] == pytest.approx(0, abs=0.001)
        finally:
            await engine.dispose()

    asyncio.run(run())
