"""Stable public facility identities and typed source-specific registration facts."""

from datetime import datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
            "daily_flat_fee_krw",
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
    daily_flat_fee_krw: Mapped[Decimal | None] = mapped_column(Numeric)
    operation_payload_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_payloads.payload_id"))
    operation_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_processor_version: Mapped[str | None] = mapped_column(Text)
    management_type_code: Mapped[str | None] = mapped_column(Text)
    management_type_name: Mapped[str | None] = mapped_column(Text)
    fee_notes: Mapped[str | None] = mapped_column(Text)
    base_usable: Mapped[bool] = mapped_column(Boolean, server_default="false")
    additional_usable: Mapped[bool] = mapped_column(Boolean, server_default="false")
    parking_type_code: Mapped[str | None] = mapped_column(Text)
    parking_type_name: Mapped[str | None] = mapped_column(Text)
    operation_code: Mapped[str | None] = mapped_column(Text)
    operation_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    payment_code: Mapped[str | None] = mapped_column(Text)
    payment_name: Mapped[str | None] = mapped_column(Text)
    is_paid: Mapped[bool | None] = mapped_column(Boolean)
    night_free_open_code: Mapped[str | None] = mapped_column(Text)
    night_free_open_name: Mapped[str | None] = mapped_column(Text)
    night_free_open: Mapped[bool | None] = mapped_column(Boolean)
    saturday_payment_code: Mapped[str | None] = mapped_column(Text)
    saturday_payment_name: Mapped[str | None] = mapped_column(Text)
    saturday_is_paid: Mapped[bool | None] = mapped_column(Boolean)
    holiday_payment_code: Mapped[str | None] = mapped_column(Text)
    holiday_payment_name: Mapped[str | None] = mapped_column(Text)
    holiday_is_paid: Mapped[bool | None] = mapped_column(Boolean)
    management_group_id: Mapped[str | None] = mapped_column(Text)
    realtime_link_code: Mapped[str | None] = mapped_column(Text)
    realtime_link_name: Mapped[str | None] = mapped_column(Text)
    sharing_code: Mapped[str | None] = mapped_column(Text)
    sharing_notes: Mapped[str | None] = mapped_column(Text)
    is_shared: Mapped[bool | None] = mapped_column(Boolean)
    hours_notes: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(Text)

    operating_hours: Mapped[list[ParkingOperatingHour]] = relationship(
        lazy="raise",
        order_by="ParkingOperatingHour.day_type, ParkingOperatingHour.period_no",
        passive_deletes=True,
    )


class ParkingOperatingHour(Base):
    __tablename__ = "parking_operating_hours"
    __table_args__ = (
        CheckConstraint(
            "day_type IN ('weekday','weekend','holiday','monday','tuesday','wednesday',"
            "'thursday','friday','saturday','sunday','sunday_holiday')",
            name="day_type",
        ),
        CheckConstraint("period_no >= 1", name="period_no"),
        CheckConstraint("state IN ('valid','missing','invalid','unknown')", name="state"),
        CheckConstraint("end_day_offset IN (0,1)", name="end_day_offset"),
        CheckConstraint(
            "state <> 'valid' OR (opens_at IS NOT NULL AND closes_at IS NOT NULL "
            "AND end_day_offset IS NOT NULL AND "
            "(end_day_offset = 1 OR closes_at > opens_at))",
            name="valid_interval",
        ),
        *processing_checks(),
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("parking_source_records.source_record_id", ondelete="CASCADE"), primary_key=True
    )
    day_type: Mapped[str] = mapped_column(Text, primary_key=True)
    period_no: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    end_day_offset: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    # Old strings are preserved as unconfirmed evidence, never guessed into intervals.
    source_text: Mapped[str | None] = mapped_column(Text)
    payload_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_payloads.payload_id"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str | None] = mapped_column(Text)


class ParkingFieldIssue(Base):
    __tablename__ = "parking_field_issues"
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("parking_source_records.source_record_id", ondelete="CASCADE"), primary_key=True
    )
    field_path: Mapped[str] = mapped_column(Text, primary_key=True)
    reason_code: Mapped[str] = mapped_column(Text)
    payload_id: Mapped[UUID] = mapped_column(ForeignKey("raw_payloads.payload_id"))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(Text)


class ParkingAvailabilityLatest(Base):
    """Latest accepted observation and latest attempted update, with independent clocks."""

    __tablename__ = "parking_availability_latest"
    __table_args__ = (
        CheckConstraint("scope = 'provider_record'", name="scope"),
        CheckConstraint("method IN ('reported','calculated')", name="method"),
        CheckConstraint("total_spaces >= 0", name="capacity"),
        CheckConstraint("occupied_spaces BETWEEN 0 AND total_spaces", name="occupied"),
        CheckConstraint("reported_available_spaces BETWEEN 0 AND total_spaces", name="reported"),
        CheckConstraint(
            "calculated_available_spaces BETWEEN 0 AND total_spaces", name="calculated"
        ),
        CheckConstraint(
            "payload_id IS NULL OR (observed_at IS NOT NULL AND received_at IS NOT NULL "
            "AND total_spaces IS NOT NULL AND processed_at IS NOT NULL "
            "AND processor_version IS NOT NULL AND observed_at <= received_at AND "
            "((method = 'reported' AND reported_available_spaces IS NOT NULL "
            "AND occupied_spaces IS NULL AND calculated_available_spaces IS NULL) OR "
            "(method = 'calculated' AND occupied_spaces IS NOT NULL "
            "AND calculated_available_spaces IS NOT NULL AND reported_available_spaces IS NULL "
            "AND calculated_available_spaces = total_spaces - occupied_spaces)))",
            name="observation",
        ),
        CheckConstraint("NOT usable OR payload_id IS NOT NULL", name="usable"),
        CheckConstraint(
            "attempt_provision_state IN ('provided','not_provided','unknown')", name="provision"
        ),
        CheckConstraint(
            "attempt_status IN ('accepted','not_provided','unknown','invalid','stale',"
            "'conflict','scope_unverified')",
            name="attempt_status",
        ),
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("parking_source_records.source_record_id", ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    total_spaces: Mapped[int | None] = mapped_column(Integer)
    occupied_spaces: Mapped[int | None] = mapped_column(Integer)
    reported_available_spaces: Mapped[int | None] = mapped_column(Integer)
    calculated_available_spaces: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_payloads.payload_id"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str | None] = mapped_column(Text)
    usable: Mapped[bool] = mapped_column(Boolean, server_default="false")
    reason_code: Mapped[str | None] = mapped_column(Text)
    attempt_payload_id: Mapped[UUID] = mapped_column(ForeignKey("raw_payloads.payload_id"))
    attempt_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_provision_code: Mapped[str | None] = mapped_column(Text)
    attempt_provision_name: Mapped[str | None] = mapped_column(Text)
    attempt_provision_state: Mapped[str] = mapped_column(Text)
    attempt_total_spaces: Mapped[int | None] = mapped_column(Integer)
    attempt_occupied_spaces: Mapped[int | None] = mapped_column(Integer)
    attempt_reported_available_spaces: Mapped[int | None] = mapped_column(Integer)
    attempt_calculated_available_spaces: Mapped[int | None] = mapped_column(Integer)
    attempt_status: Mapped[str] = mapped_column(Text)
    attempt_reason_code: Mapped[str | None] = mapped_column(Text)
    attempt_processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_processor_version: Mapped[str] = mapped_column(Text)
