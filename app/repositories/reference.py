"""Typed collection prerequisites and official road references; no payload file reads."""

import json

from sqlalchemy import func, select

from app.models import (
    PedestrianSensor,
    PopulationArea,
    RegionBoundary,
    RoadLink,
    RoadTrafficAttemptLatest,
)


def current_roads():
    return (
        RoadLink.source == "topis_geometry",
        RoadLink.is_active.is_(True),
        RoadLink.source_version.is_not(None),
        RoadLink.processed_at.is_not(None),
    )


def current_seoul_boundary():
    return (
        RegionBoundary.code_system == "sgis",
        RegionBoundary.region_code == "11",
        RegionBoundary.source == "sgis_seoul_boundary",
        RegionBoundary.is_active.is_(True),
        RegionBoundary.boundary_version.is_not(None),
        RegionBoundary.processed_at.is_not(None),
    )


class ReferenceRepository:
    def __init__(self, session):
        self.session = session

    async def has_place_records(self):
        # Inactive rows still represent an intentional import. Bootstrap must not
        # replace that reference set with the files bundled in a local checkout.
        return bool(await self.session.scalar(select(select(PopulationArea).exists())))

    async def citydata_areas(self):
        rows = await self.session.execute(
            select(PopulationArea.area_code, PopulationArea.name)
            .where(PopulationArea.is_active.is_(True))
            .order_by(PopulationArea.area_code)
        )
        return dict(rows.all())

    async def sensor_reference(self):
        rows = (
            await self.session.execute(
                select(
                    PedestrianSensor,
                    func.ST_X(PedestrianSensor.geometry),
                    func.ST_Y(PedestrianSensor.geometry),
                )
                .where(
                    PedestrianSensor.source == "sdot_people", PedestrianSensor.is_active.is_(True)
                )
                .order_by(PedestrianSensor.sensor_code)
            )
        ).all()
        if not rows:
            return None
        versions = {sensor.source_version for sensor, _, _ in rows}
        boundaries = {sensor.boundary_version for sensor, _, _ in rows}
        if len(versions) != 1 or len(boundaries) != 1:
            raise ValueError("inconsistent_sensor_reference")
        return {
            "locations": {
                sensor.sensor_code: dict(
                    id=sensor.sensor_code,
                    provider_sensor_id=sensor.provider_sensor_id,
                    device_serial=sensor.device_serial,
                    address=sensor.address,
                    lng=lng,
                    lat=lat,
                )
                for sensor, lng, lat in rows
            },
            "metadata": {
                "version": next(iter(versions)),
                "boundary_version": next(iter(boundaries)),
            },
            "quality": {
                "received": len(rows),
                "accepted": len(rows),
                "rejected": 0,
                "duplicates": 0,
            },
        }

    async def topis_links(self):
        return list(
            await self.session.scalars(
                select(RoadLink.link_id).where(*current_roads()).order_by(RoadLink.link_id)
            )
        )

    async def topis_attempt_times(self):
        rows = await self.session.execute(
            select(RoadTrafficAttemptLatest.link_id, RoadTrafficAttemptLatest.attempted_at)
        )
        return {link: when.isoformat() for link, when in rows}

    async def seoul_boundary(self):
        row = (
            await self.session.execute(
                select(
                    func.ST_AsGeoJSON(RegionBoundary.geometry),
                    RegionBoundary.source_version,
                    RegionBoundary.boundary_version,
                ).where(*current_seoul_boundary())
            )
        ).first()
        if row is None:
            return None
        year = int(row[1]) if row[1] and row[1].isdigit() else None
        return {
            "geometry": json.loads(row[0]),
            "year": year,
            "version": row[2],
        }

    async def has_prerequisite(self, source):
        if source == "topis_geometry" or source.startswith("topis_geometry:"):
            query = select(RoadLink.link_id).where(*current_roads())
        elif source == "sgis_seoul_boundary":
            query = select(RegionBoundary.region_code).where(*current_seoul_boundary())
        elif source == "citydata_places":
            query = select(PopulationArea.area_code).where(
                PopulationArea.is_active.is_(True), PopulationArea.source_version.is_not(None)
            )
        elif source == "sdot_locations":
            boundary = select(RegionBoundary.boundary_version).where(*current_seoul_boundary())
            query = select(PedestrianSensor.sensor_code).where(
                PedestrianSensor.source == "sdot_people",
                PedestrianSensor.is_active.is_(True),
                PedestrianSensor.source_version.is_not(None),
                PedestrianSensor.boundary_version == boundary.scalar_subquery(),
            )
        else:
            raise ValueError("unsupported_collection_prerequisite")
        return bool(await self.session.scalar(select(query.exists())))
