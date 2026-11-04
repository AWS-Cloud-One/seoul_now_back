"""P04 parking registration response; live availability is unsupported."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.schemas.common import ApiEnvelope, ContractModel, ErrorResponse, Information


class ParkingFees(ContractModel):
    base_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    base_fee_krw: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    additional_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    additional_fee_krw: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    daily_max_krw: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    notes: str | None = None


class OperatingHours(ContractModel):
    weekdays: str | None = None
    saturday: str | None = None
    sunday_holidays: str | None = None
    notes: str | None = None


class RegisteredParking(ContractModel):
    address: str | None = None
    registered_capacity: int | None = Field(default=None, ge=0, strict=True)
    fees: ParkingFees | None = None
    operating_hours: OperatingHours | None = None


class ParkingSourceRecord(ContractModel):
    source_record_id: UUID
    source: str
    reference_date: date | None
    registered: Information[RegisteredParking]


class UnsupportedAvailability(ContractModel):
    value: None = None
    status: Literal["unsupported"] = "unsupported"
    freshness: Literal["not_applicable"] = "not_applicable"
    observed_at: None = None
    collected_at: None = None
    source: None = None
    reason_code: Literal["NOT_SUPPORTED"] = "NOT_SUPPORTED"


class ParkingDetail(ContractModel):
    parking_id: UUID
    identity_status: Literal["verified", "unverified"]
    checked_at: AwareDatetime
    records: list[ParkingSourceRecord]
    availability: UnsupportedAvailability = Field(default_factory=UnsupportedAvailability)


class ParkingDetailResponse(ApiEnvelope[ParkingDetail]):
    data: ParkingDetail
    error: None = None


class ParkingErrorResponse(ErrorResponse):
    pass
