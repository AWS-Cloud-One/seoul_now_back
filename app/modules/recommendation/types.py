"""Internal recommendation candidates; public requests keep the API_SPEC models."""

from datetime import datetime

from pydantic import Field

from app.schemas.common import ContractModel
from app.schemas.parking_nearby import ParkingSummary
from app.schemas.pending import Place


class Candidate(ContractModel):
    place: Place
    collected_at: datetime
    duration_seconds: int | None = Field(default=None, strict=True, ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    route_collected_at: datetime | None = None
    parking: ParkingSummary | None = None
