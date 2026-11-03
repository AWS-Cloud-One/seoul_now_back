"""P02 nearby registered facilities; no live vacancy inference."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.schemas.common import ApiEnvelope, ContractModel
from app.schemas.location import Coordinates
from app.schemas.parking_detail import ParkingSourceResponse, UnsupportedAvailability


class NearbyFacility(ContractModel):
    parking_id: UUID
    identity_status: Literal["verified", "unverified"]
    name: str
    coordinates: Coordinates
    distance_meters: float = Field(ge=0)
    records: list[ParkingSourceResponse]
    availability: UnsupportedAvailability = Field(default_factory=UnsupportedAvailability)


class ParkingSummary(ContractModel):
    status: Literal["available", "unavailable", "error"]
    reason_code: str | None
    checked_at: AwareDatetime
    search_radius_meters: Literal[300, 500, 1000] | None
    nearest_distance_meters: float | None = Field(ge=0)
    items: list[NearbyFacility]
    total_count: int | None = Field(ge=0)
    next_cursor: str | None


class NearbyParkingData(ContractModel):
    coordinates: Coordinates
    parking: ParkingSummary


class NearbyParkingResponse(ApiEnvelope[NearbyParkingData]):
    data: NearbyParkingData
    error: None = None
