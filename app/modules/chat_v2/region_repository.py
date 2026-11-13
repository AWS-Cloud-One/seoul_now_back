"""Read official geometries without treating hotspot observations as recommendations."""

import json

from sqlalchemy import func, select

from app.models import AdministrativeArea, PopulationArea, RegionBoundary
from app.modules.recommendation.areas import SearchArea
from app.repositories.recommendation_geography import current_areas


def shape(identifier, name, version, geometry, lon, lat, source):
    return SearchArea(
        area_id=identifier,
        name=name,
        source=source,
        geometry_version=version,
        center={"longitude": lon, "latitude": lat},
        polygons=json.loads(geometry)["coordinates"],
    )


async def official_areas(db):
    center = func.ST_PointOnSurface(PopulationArea.geometry)
    rows = await db.execute(
        select(
            PopulationArea.area_code,
            PopulationArea.name,
            PopulationArea.source_version,
            func.ST_AsGeoJSON(PopulationArea.geometry),
            func.ST_X(center),
            func.ST_Y(center),
        ).where(
            PopulationArea.source == "citydata_place_boundaries",
            PopulationArea.source_version.is_not(None),
            PopulationArea.processed_at.is_not(None),
            PopulationArea.is_active.is_(True),
            func.ST_IsValid(PopulationArea.geometry),
        )
    )
    return [shape(*row, "citydata_place_boundaries") for row in rows]


async def seoul_shape(db):
    geometry = await db.scalar(
        select(func.ST_AsGeoJSON(RegionBoundary.geometry)).where(
            RegionBoundary.code_system == "sgis",
            RegionBoundary.region_code == "11",
            RegionBoundary.is_active.is_(True),
            RegionBoundary.processed_at.is_not(None),
        )
    )
    if not geometry:
        geometry = await db.scalar(
            select(
                func.ST_AsGeoJSON(func.ST_Multi(func.ST_Union(AdministrativeArea.geometry)))
            ).where(*current_areas())
        )
    if not geometry:
        return None
    return shape("11", "서울", "current", geometry, 126.978, 37.5665, "sgis_areas")


def bounding_rect(area):
    points = [p for polygon in area.polygons for ring in polygon for p in ring]
    return ",".join(
        str(v)
        for v in (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )
    )
