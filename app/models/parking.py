"""Stable public facility identities and typed source-specific registration facts."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditColumns, Base, SourceColumns, nonblank, nonnegative, processing_checks


class ParkingFacility(AuditColumns, Base):
    __tablename__ = "parking_facilities"
    __table_args__ = (
        *nonblank("name"),
        CheckConstraint("identity_status IN ('verified','unverified')", name="identity_status"),
        CheckConstraint("NOT ST_IsEmpty(location::geometry)", name="nonempty_location"),
    )

    parking_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    location = mapped_column(Geography("POINT", srid=4326), nullable=False)
    identity_status: Mapped[str] = mapped_column(Text, server_default="unverified")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ParkingSourceRecord(AuditColumns, SourceColumns, Base):
    __tablename__ = "parking_source_records"
    __table_args__ = (
        *processing_checks(),
        *nonblank("legacy_record_id", "provider_record_id", "name"),
        CheckConstraint("registered_capacity >= 0", name="capacity_nonnegative"),
        CheckConstraint("NOT ST_IsEmpty(location::geometry)", name="nonempty_location"),
        *nonnegative(
            "base_minutes",
            "base_fee_krw",
            "additional_minutes",
            "additional_fee_krw",
            "daily_max_krw",
        ),
    )

    source_record_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    parking_id: Mapped[UUID] = mapped_column(
        ForeignKey("parking_facilities.parking_id", ondelete="RESTRICT"), index=True
    )
    # Transition identity only. A provider code is NOT unique across physical spaces.
    legacy_record_id: Mapped[str | None] = mapped_column(Text, unique=True)
    provider_record_id: Mapped[str | None] = mapped_column(Text, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Geography("POINT", srid=4326), nullable=True)
    address: Mapped[str | None] = mapped_column(Text)
    registered_capacity: Mapped[int | None] = mapped_column(Integer)
    base_minutes: Mapped[Decimal | None] = mapped_column(Numeric)
    base_fee_krw: Mapped[Decimal | None] = mapped_column(Numeric)
    additional_minutes: Mapped[Decimal | None] = mapped_column(Numeric)
    additional_fee_krw: Mapped[Decimal | None] = mapped_column(Numeric)
    daily_max_krw: Mapped[Decimal | None] = mapped_column(Numeric)
    fee_notes: Mapped[str | None] = mapped_column(Text)
    weekday_hours: Mapped[str | None] = mapped_column(Text)
    saturday_hours: Mapped[str | None] = mapped_column(Text)
    sunday_holiday_hours: Mapped[str | None] = mapped_column(Text)
    hours_notes: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(Text)
