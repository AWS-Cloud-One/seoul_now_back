"""Parking positions and current availability from typed source records."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import func, select

from app.models import ParkingAvailabilityLatest, ParkingSourceRecord

AVAILABILITY_MAX_AGE = timedelta(hours=1)


@dataclass(frozen=True)
class ParkingAvailability:
    source_record_id: UUID
    source: str
    available_spaces: int | None
    total_spaces: int | None
    usable: bool
    observed_at: datetime | None
    received_at: datetime | None
    time_basis: str | None
    expires_at: datetime | None
    reason_code: str | None


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

    async def availability(self, source_record_id, *, now=None):
        """Keep historical facts, but expose counts only while usable at the requested time."""
        now = now or datetime.now(UTC)
        if now.utcoffset() is None:
            raise ValueError("availability_time_requires_timezone")
        row = (
            await self.session.execute(
                select(ParkingAvailabilityLatest, ParkingSourceRecord.source)
                .join(ParkingSourceRecord)
                .where(
                    ParkingAvailabilityLatest.source_record_id == source_record_id,
                    ParkingSourceRecord.is_active.is_(True),
                )
            )
        ).first()
        if row is None:
            return None
        state, source = row
        total, received = state.total_spaces, state.received_at
        observed, usable, reason = state.observed_at, state.usable, state.reason_code
        available = (
            state.reported_available_spaces
            if state.method == "reported"
            else state.calculated_available_spaces
        )
        basis, expires = "observed_at" if observed is not None else None, None
        if source == "national_parking" and observed is None:
            # Keep source-timestamped observations separate from the latest received counts.
            total, available = state.attempt_total_spaces, state.attempt_reported_available_spaces
            received, basis = state.attempt_received_at, "received_at"
            reason = state.attempt_reason_code
            if state.reason_code in {"AMBIGUOUS_AVAILABILITY_SCOPE", "SAME_TIME_CONFLICT"}:
                reason = state.reason_code
            usable = (
                state.attempt_status == "unknown"
                and reason == "SOURCE_TIME_UNKNOWN"
                and state.attempt_provision_state == "provided"
                and total is not None
                and available is not None
                and 0 <= available <= total
            )
            if received is None:
                usable, reason = False, "RECEIPT_TIME_UNKNOWN"
            else:
                expires = received + AVAILABILITY_MAX_AGE
                if received > now:
                    usable, reason = False, "FUTURE_TIMESTAMP"
                elif now > expires:
                    usable, reason = False, "STALE_DATA"
        else:
            if observed is None:
                usable, reason = False, reason or "SOURCE_TIME_UNKNOWN"
            else:
                expires = observed + AVAILABILITY_MAX_AGE
                if observed > now:
                    usable, reason = False, "FUTURE_TIMESTAMP"
                elif now > expires:
                    usable, reason = False, "STALE_DATA"
            # A later explicit withdrawal overrides the retained historical observation.
            if state.attempt_provision_state == "not_provided":
                usable, reason = False, "NOT_PROVIDED"
        return ParkingAvailability(
            source_record_id=source_record_id,
            source=source,
            available_spaces=available if usable else None,
            total_spaces=total,
            usable=usable,
            observed_at=observed,
            received_at=received,
            time_basis=basis,
            expires_at=expires,
            reason_code=reason,
        )
