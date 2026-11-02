"""P02 spatial lookup against published facilities, separate from legacy sorting."""

from geoalchemy2 import Geography
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.map import MapFeature, MapLayerVersion, ParkingFacility, ParkingSourceRecord


class ParkingNearbyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_version(self):
        return await self.session.scalar(
            select(MapLayerVersion)
            .where(
                MapLayerVersion.layer == "parking",
            )
            .with_for_update(read=True)
        )

    @staticmethod
    def spatial_query(coordinates, radius):
        point = func.ST_SetSRID(
            func.ST_MakePoint(
                coordinates.longitude,
                coordinates.latitude,
            ),
            4326,
        ).cast(Geography)
        location = MapFeature.geometry.cast(Geography)
        distance = func.ST_Distance(location, point)
        statement = (
            select(ParkingFacility)
            .join(
                MapFeature,
                (
                    (MapFeature.layer == "parking")
                    & (MapFeature.feature_id == cast(ParkingFacility.id, String))
                ),
            )
            .where(
                ParkingFacility.is_active.is_(True),
                MapFeature.is_active.is_(True),
                func.ST_DWithin(location, point, radius),
            )
        )
        return statement, distance

    async def summarize_radius(self, coordinates, radius):
        statement, distance = self.spatial_query(coordinates, radius)
        result = await self.session.execute(
            statement.with_only_columns(
                func.count(ParkingFacility.id),
                func.min(distance),
            )
        )
        return result.one()

    async def list_facilities(self, coordinates, radius):
        statement, distance = self.spatial_query(coordinates, radius)
        rows = await self.session.execute(
            statement.with_only_columns(
                ParkingFacility.id,
                ParkingFacility.identity_status,
                MapFeature.properties,
                func.ST_Y(MapFeature.geometry),
                func.ST_X(MapFeature.geometry),
                distance,
            ).order_by(distance, ParkingFacility.id)
        )
        return rows.all()

    async def list_records(self, ids):
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(ParkingSourceRecord)
            .where(
                ParkingSourceRecord.parking_id.in_(ids),
                ParkingSourceRecord.is_active.is_(True),
            )
            .order_by(ParkingSourceRecord.source, ParkingSourceRecord.id)
        )
        grouped = {key: [] for key in ids}
        for record in rows:
            grouped[record.parking_id].append(record)
        return grouped
