"""Read registered facilities without collecting or merging provider data."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.map import ParkingFacility, ParkingSourceRecord


class ParkingDetailRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_facility(self, parking_id: UUID) -> ParkingFacility | None:
        # Hold a shared lock while constructing the response so a concurrent
        # publisher cannot deactivate the facility and replace its records midway.
        return await self.session.scalar(
            select(ParkingFacility)
            .where(ParkingFacility.id == parking_id)
            .with_for_update(read=True)
        )

    async def list_records(self, parking_id: UUID) -> list[ParkingSourceRecord]:
        records = await self.session.scalars(
            select(ParkingSourceRecord)
            .where(
                ParkingSourceRecord.parking_id == parking_id,
                ParkingSourceRecord.is_active.is_(True),
            )
            .order_by(ParkingSourceRecord.source, ParkingSourceRecord.id)
        )
        return list(records)
