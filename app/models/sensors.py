"""Sensor installation facts and independent last-valid / latest-attempt state."""

from datetime import date, datetime
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditColumns, Base, nonblank


class PedestrianSensor(AuditColumns, Base):
    __tablename__ = "pedestrian_sensors"
    __table_args__ = (
        *nonblank(
            "source",
            "sensor_code",
            "provider_sensor_id",
            "device_serial",
            "address",
            "source_version",
            "boundary_version",
            "processor_version",
        ),
        CheckConstraint("sensor_code ~ '^[1-9][0-9]*$'", name="sensor_code_format"),
        CheckConstraint(
            "NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry) "
            "AND ST_X(geometry) BETWEEN -180 AND 180 AND ST_Y(geometry) BETWEEN -90 AND 90",
            name="valid_point",
        ),
    )
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code"), primary_key=True
    )
    sensor_code: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_sensor_id: Mapped[str] = mapped_column(Text)
    device_serial: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    geometry = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    source_version: Mapped[str] = mapped_column(Text)
    reference_date: Mapped[date | None] = mapped_column(Date)
    reference_payload_id: Mapped[UUID] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    boundary_payload_id: Mapped[UUID] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    boundary_version: Mapped[str] = mapped_column(Text)
    last_payload_id: Mapped[UUID] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    reference_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(Text)


class PedestrianSensorLatest(AuditColumns, Base):
    __tablename__ = "pedestrian_sensor_latest"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source", "sensor_code"],
            ["pedestrian_sensors.source", "pedestrian_sensors.sensor_code"],
            ondelete="RESTRICT",
        ),
        *nonblank(
            "data_no",
            "provider_sensor_id",
            "reference_version",
            "processor_version",
            "attempt_processor_version",
        ),
        CheckConstraint("count_10min >= 0 AND attempt_count_10min >= 0", name="nonnegative_counts"),
        CheckConstraint(
            "attempt_status IN ('valid','partial','unobserved','invalid','conflicting',"
            "'ambiguous_data_no','failed','held')",
            name="attempt_status",
        ),
        CheckConstraint(
            "num_nonnulls(count_10min, observed_at, payload_id, data_no, received_at, "
            "provider_collected_at, reference_version, reference_payload_id, processed_at, "
            "processor_version, provider_sensor_id) IN (0,11)",
            name="observation_provenance",
        ),
        CheckConstraint(
            "observed_at <= provider_collected_at AND provider_collected_at <= received_at",
            name="observation_times",
        ),
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    sensor_code: Mapped[str] = mapped_column(Text, primary_key=True)
    count_10min: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_no: Mapped[str | None] = mapped_column(Text)
    provider_sensor_id: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(Text)
    payload_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    reference_version: Mapped[str | None] = mapped_column(Text)
    reference_payload_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str | None] = mapped_column(Text)
    attempt_status: Mapped[str] = mapped_column(Text)
    attempt_reason: Mapped[str | None] = mapped_column(Text)
    attempt_count_10min: Mapped[int | None] = mapped_column(BigInteger)
    attempt_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_provider_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_data_no: Mapped[str | None] = mapped_column(Text)
    attempt_payload_id: Mapped[UUID] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT")
    )
    attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_processor_version: Mapped[str] = mapped_column(Text)
