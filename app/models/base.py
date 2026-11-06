"""Shared normalized storage constraints and provenance columns."""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base as Base


def nonblank(*columns):
    return tuple(CheckConstraint(f"btrim({c}) <> ''", name=f"{c}_nonblank") for c in columns)


def nonnegative(*columns):
    return tuple(
        CheckConstraint(f"{c} >= 0 AND {c} < 'Infinity'::numeric", name=f"{c}_finite")
        for c in columns
    )


def processing_checks():
    return (
        *nonblank("processor_version"),
        CheckConstraint(
            "(processor_version IS NULL) = (processed_at IS NULL)", name="processing_pair"
        ),
    )


class AuditColumns:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # ORM/Core updates using this metadata emit now(). Direct SQL must set updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SourceColumns:
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT")
    )
    source_version: Mapped[str | None] = mapped_column(Text)
    reference_date: Mapped[date | None] = mapped_column(Date)
    # Legacy rows may lack processing provenance; new writers must supply both.
    processor_version: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ObservationColumns:
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
