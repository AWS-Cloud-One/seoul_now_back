from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import ParkingFacility, ParkingSourceRecord


class ParkingDetailRepository:
    def __init__(self, session):
        self.session = session

    async def get_facility(self, parking_id):
        return await self.session.scalar(
            select(ParkingFacility).where(ParkingFacility.parking_id == parking_id)
        )

    async def list_records(self, parking_id):
        return list(
            await self.session.scalars(
                select(ParkingSourceRecord)
                .options(selectinload(ParkingSourceRecord.operating_hours))
                .where(
                    ParkingSourceRecord.parking_id == parking_id,
                    ParkingSourceRecord.is_active.is_(True),
                )
                .order_by(ParkingSourceRecord.source, ParkingSourceRecord.source_record_id)
            )
        )
