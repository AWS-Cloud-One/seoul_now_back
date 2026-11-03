"""Historical provider responses and the observations extracted from each response."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExternalApiResponse(Base):
    __tablename__ = "external_api_responses"
    __table_args__ = (
        Index("ix_external_api_responses_source_collected", "source", "collected_at"),
        Index("ix_external_api_responses_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    # Logical operation name, never a URL containing credentials.
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Parsed response with credentials removed; no headers or request URLs.
    payload: Mapped[dict | list] = mapped_column(JSONB, nullable=False)


class ExternalApiObservation(Base):
    __tablename__ = "external_api_observations"
    __table_args__ = (
        UniqueConstraint("response_id", "record_key", name="uq_external_api_observation_record"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_api_observation_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_api_observation_longitude"),
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="ck_api_observation_coordinate_pair"
        ),
        Index("ix_external_api_observations_record_time", "record_key", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    response_id: Mapped[UUID] = mapped_column(
        ForeignKey("external_api_responses.id"), nullable=False
    )
    # Stable provider identity, including a subrecord suffix when necessary.
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300))
    # Unknown source time stays NULL; never substitute collection time.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    # Measurements retain their names, units and provider-specific meaning.
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
