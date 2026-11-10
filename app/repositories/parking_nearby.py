"""P02 spatial lookup against published facilities, separate from legacy sorting."""

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import MapFeature, MapLayerVersion, ParkingFacility, ParkingSourceRecord


class ParkingNearbyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_version(self):
        return await self.session.scalar(
            select(MapLayerVersion).where(MapLayerVersion.layer == "parking")
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
                    & (MapFeature.parking_id == ParkingFacility.parking_id)
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
                func.count(ParkingFacility.parking_id),
                func.min(distance),
            )
        )
        return result.one()

    async def list_facilities(self, coordinates, radius, *, limit=None, offset=0, name=None):
        statement, distance = self.spatial_query(coordinates, radius)
        if name:
            statement = statement.where(ParkingFacility.name.contains(name, autoescape=True))
        query = (
            statement.with_only_columns(
                ParkingFacility.parking_id,
                ParkingFacility.identity_status,
                func.jsonb_build_object("name", MapFeature.name),
                func.ST_Y(MapFeature.geometry),
                func.ST_X(MapFeature.geometry),
                distance,
            )
            .order_by(distance, ParkingFacility.parking_id)
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        rows = await self.session.execute(query)
        return rows.all()

    async def list_records(self, ids):
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(ParkingSourceRecord)
            .options(selectinload(ParkingSourceRecord.operating_hours))
            .where(
                ParkingSourceRecord.parking_id.in_(ids),
                ParkingSourceRecord.is_active.is_(True),
            )
            .order_by(ParkingSourceRecord.source, ParkingSourceRecord.source_record_id)
        )
        grouped = {key: [] for key in ids}
        for record in rows:
            grouped[record.parking_id].append(record)
        return grouped
