"""P04 detail rules; no external HTTP calls or live vacancy inference."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.repositories.parking_detail import ParkingDetailRepository
from app.schemas.parking_detail import ParkingDetail
from app.services.parking_registration import registered_information


class ParkingDetailService:
    def __init__(self, repository: ParkingDetailRepository):
        self.repository = repository

    async def get_detail(self, parking_id: UUID) -> ParkingDetail:
        try:
            facility = await self.repository.get_facility(parking_id)
            if facility is None or not facility.is_active:
                raise ServiceError("PARKING_NOT_FOUND", "제공되는 주차장 ID가 아닙니다.")
            records = await self.repository.list_records(parking_id)
            return ParkingDetail(
                parking_id=facility.parking_id,
                identity_status=facility.identity_status,
                checked_at=datetime.now(UTC),
                records=[
                    {
                        "source_record_id": record.source_record_id,
                        "source": record.source,
                        "reference_date": record.reference_date,
                        "registered": registered_information(record),
                    }
                    for record in records
                ],
            )
        except SQLAlchemyError, ValidationError:
            # Bad stored detail is a data failure, never a nonexistent facility
            # or a successful empty list. Keep database/provider details private.
            raise ServiceError("DATA_UNAVAILABLE", "주차 등록정보를 조회할 수 없습니다.") from None
