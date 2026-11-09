"""Read-only administrative, published parking and official road-ID facts."""

import json
from collections import defaultdict
from datetime import UTC, datetime

from geoalchemy2 import Geography
from sqlalchemy import and_, case, cast, func, select

from app.models import (
    AdministrativeArea,
    MapFeature,
    MapLayerVersion,
    ParkingFacility,
    PopulationArea,
    RoadAreaMapping,
    RoadLink,
    RoadLinkMapping,
)
from app.modules.recommendation.areas import SearchArea
from app.repositories.reference import current_roads


def point(coordinates):
    return func.ST_SetSRID(func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326)


def current_areas():
    return (
        AdministrativeArea.source == "sgis_areas",
        AdministrativeArea.code_system == "sgis",
        AdministrativeArea.sigungu_code.startswith("11"),
        AdministrativeArea.is_active.is_(True),
        AdministrativeArea.processed_at.is_not(None),
    )


class RecommendationGeographyRepository:
    def __init__(self, session):
        self.session = session

    async def ready_version(self, layer):
        return await self.session.scalar(
            select(MapLayerVersion.geometry_version).where(
                MapLayerVersion.layer == layer, MapLayerVersion.is_ready.is_(True)
            )
        )

    async def areas(self):
        async with self.session.begin_nested():
            center = func.ST_PointOnSurface(AdministrativeArea.geometry)
            rows = await self.session.execute(
                select(
                    AdministrativeArea.dong_code,
                    AdministrativeArea.name,
                    AdministrativeArea.boundary_version,
                    func.ST_AsGeoJSON(AdministrativeArea.geometry),
                    func.ST_X(center),
                    func.ST_Y(center),
                )
                .where(*current_areas())
                .order_by(AdministrativeArea.dong_code)
            )
            return [
                SearchArea(
                    area_id=code,
                    name=name,
                    source="sgis_areas",
                    geometry_version=version,
                    center={"longitude": lon, "latitude": lat},
                    polygons=json.loads(geometry)["coordinates"],
                )
                for code, name, version, geometry, lon, lat in rows
            ]

    async def parking_counts(self):
        async with self.session.begin_nested():
            if not await self.ready_version("parking"):
                return None
            rows = await self.session.execute(
                select(AdministrativeArea.dong_code, func.count(MapFeature.parking_id))
                .join(
                    MapFeature,
                    and_(
                        MapFeature.layer == "parking",
                        MapFeature.is_active.is_(True),
                        func.ST_Covers(AdministrativeArea.geometry, MapFeature.geometry),
                    ),
                )
                .join(ParkingFacility, ParkingFacility.parking_id == MapFeature.parking_id)
                .where(*current_areas(), ParkingFacility.is_active.is_(True))
                .group_by(AdministrativeArea.dong_code)
            )
            return dict(rows.all()), datetime.now(UTC)

    @staticmethod
    def road_query(*columns, valid=True):
        # An unmatched/stale correspondence remains NULL, not a silently omitted road.
        return (
            select(
                *columns,
                case(
                    (
                        and_(
                            valid,
                            MapFeature.feature_id.is_not(None),
                            RoadLinkMapping.processed_at == RoadLink.processed_at,
                            RoadLinkMapping.sequence_status != "ambiguous",
                        ),
                        RoadLinkMapping.standard_link_id,
                    )
                ),
            )
            .select_from(RoadLink)
            .outerjoin(
                MapFeature,
                and_(
                    MapFeature.layer == "roads",
                    MapFeature.road_link_id == RoadLink.link_id,
                    MapFeature.is_active.is_(True),
                    func.ST_Equals(MapFeature.geometry, RoadLink.geometry),
                ),
            )
            .outerjoin(
                RoadLinkMapping,
                and_(
                    RoadLinkMapping.service_link_id == RoadLink.link_id,
                    RoadLinkMapping.source == "topis_standard_mapping",
                ),
            )
            .where(*current_roads())
        )

    async def area_roads(self):
        async with self.session.begin_nested():
            version = await self.ready_version("roads")
            if not version:
                return {}
            rows = await self.session.execute(
                self.road_query(
                    AdministrativeArea.dong_code,
                    valid=and_(
                        RoadAreaMapping.boundary_version == AdministrativeArea.boundary_version,
                        RoadAreaMapping.road_geometry_version == version,
                        AdministrativeArea.road_mapping_version == version,
                    ),
                )
                .join(RoadAreaMapping, RoadAreaMapping.link_id == RoadLink.link_id)
                .join(AdministrativeArea, AdministrativeArea.dong_code == RoadAreaMapping.dong_code)
                .where(*current_areas())
            )
            result = defaultdict(set)
            for code, standard_id in rows:
                result[code].add(standard_id)
            return dict(result)

    async def nearby_roads(self, coordinates, radius_m):
        async with self.session.begin_nested():
            if not await self.ready_version("roads"):
                return set()
            return set(
                await self.session.scalars(
                    self.road_query().where(
                        func.ST_DWithin(
                            cast(RoadLink.geometry, Geography),
                            cast(point(coordinates), Geography),
                            radius_m,
                        )
                    )
                )
            )

    async def hotspots(self, coordinates, *, covering, limit):
        # Hotspots only identify citydata requests. Their area-wide grade/population is unused.
        async with self.session.begin_nested():
            query = select(PopulationArea.area_code, PopulationArea.name).where(
                PopulationArea.is_active.is_(True),
                PopulationArea.source == "citydata_place_boundaries",
                PopulationArea.source_version.is_not(None),
                PopulationArea.processed_at.is_not(None),
            )
            if covering:
                query = query.where(func.ST_Covers(PopulationArea.geometry, point(coordinates)))
            rows = await self.session.execute(
                query.order_by(
                    func.ST_Distance(
                        cast(PopulationArea.geometry, Geography),
                        cast(point(coordinates), Geography),
                    ),
                    PopulationArea.area_code,
                ).limit(limit)
            )
            return list(rows.all())
