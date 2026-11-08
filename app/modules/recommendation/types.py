"""Internal recommendation candidates; public requests keep the API_SPEC models."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field

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

    # Destination-road grade supplied by a verified source; never inferred from parking.
    congestion: Literal["원활", "서행", "정체", "확인 불가"] = "확인 불가"
    congestion_observed_at: AwareDatetime | None = None
    congestion_source: str | None = None
