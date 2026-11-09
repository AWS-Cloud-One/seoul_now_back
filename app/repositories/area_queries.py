"""Part 1 population-hotspot facts; administrative dong identity stays distinct."""

from sqlalchemy import and_, func, select

from app.models import MapFeature, PopulationArea
from app.repositories.map import MapRepository


class AreaQueryRepository:
    def __init__(self, session):
        self.session = session
        self.map = MapRepository(session)

    async def area(self, area_code):
        return await self.session.scalar(
            select(PopulationArea).where(PopulationArea.area_code == area_code)
        )

    def features(self, area_code, layer):
        boundary = (
            select(PopulationArea.geometry)
            .where(PopulationArea.area_code == area_code)
            .scalar_subquery()
        )
        return select(MapFeature.feature_id).where(
            MapFeature.layer == layer,
            MapFeature.is_active.is_(True),
            func.ST_Intersects(MapFeature.geometry, boundary),
        )

    async def roads(self, area_code):
        ids = list(
            await self.session.scalars(
                self.features(area_code, "roads").order_by(MapFeature.feature_id).limit(10001)
            )
        )
        if len(ids) > 10000:
            from app.core.exceptions import ServiceError

            raise ServiceError("DATA_UNAVAILABLE", "관측 영역의 도로 조회 한도를 초과했습니다.")
        return ids, await self.map.get_observations("roads", ids)

    async def parking_count(self, area_code):
        return await self.session.scalar(
            select(func.count()).select_from(self.features(area_code, "parking").subquery())
        )

    async def population(self, area_code):
        return (await self.map.get_observations("population", [area_code])).get(area_code)

    async def populations_at(self, coordinates):
        """Exact current hotspot containment; never administrative codes or a made-up radius."""
        current = (
            PopulationArea.is_active.is_(True),
            PopulationArea.source == "citydata_place_boundaries",
            PopulationArea.source_version.is_not(None),
            PopulationArea.processed_at.is_not(None),
        )
        point = func.ST_SetSRID(
            func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
        )
        async with self.session.begin_nested():
            if not await self.session.scalar(
                select(select(PopulationArea.area_code).where(*current).exists())
            ):
                return None
            rows = (
                await self.session.execute(
                    select(
                        PopulationArea.area_code,
                        PopulationArea.name,
                        PopulationArea.source_version,
                        MapFeature.feature_id,
                    )
                    .outerjoin(
                        MapFeature,
                        and_(
                            MapFeature.layer == "population",
                            MapFeature.is_active.is_(True),
                            MapFeature.population_area_code == PopulationArea.area_code,
                            func.ST_Equals(MapFeature.geometry, PopulationArea.geometry),
                        ),
                    )
                    .where(*current, func.ST_Covers(PopulationArea.geometry, point))
                    .order_by(PopulationArea.area_code)
                    .limit(101)
                )
            ).all()
            if len(rows) > 100:
                from app.core.exceptions import ServiceError

                raise ServiceError(
                    "DATA_UNAVAILABLE", "겹치는 인구 관측 구역이 조회 한도를 넘었습니다."
                )
            ready = await self.ready("population")
            observations = (
                await self.map.get_observations(
                    "population",
                    [code for code, _, _, published in rows if published],
                )
                if ready
                else {}
            )
            return [
                {
                    "area_code": code,
                    "name": name,
                    "boundary_version": version,
                    "reason_code": "DATA_UNAVAILABLE"
                    if not ready
                    else "GEOMETRY_VERSION_MISMATCH"
                    if not published
                    else None,
                    "observation": observations.get(code),
                }
                for code, name, version, published in rows
            ]

    async def ready(self, layer):
        version = await self.map.get_version(layer)
        return bool(version is not None and version.is_ready and version.geometry_version)

    async def read_section(self, area_code, layer):
        # Isolate errors so one failed section does not abort all other reads.
        async with self.session.begin_nested():
            if not await self.ready(layer):
                return False, None
            reader = {
                "population": self.population,
                "roads": self.roads,
                "parking": self.parking_count,
            }[layer]
            return True, await reader(area_code)
