"""Registration coordinates used to attach positions during collection."""

from geoalchemy2 import Geometry
from sqlalchemy import func, select

from app.models import ParkingSourceRecord


class ParkingRepository:
    def __init__(self, session):
        self.session = session

    async def active_positions(self, source):
        r = ParkingSourceRecord
        records = await self.session.execute(
            select(
                r.provider_record_id,
                func.ST_Y(r.location.cast(Geometry)),
                func.ST_X(r.location.cast(Geometry)),
            ).where(r.source == source, r.is_active.is_(True), r.location.is_not(None))
        )
        positions = {}
        for code, lat, lon in records:
            positions.setdefault(code, set()).add((lat, lon))
        return positions
