"""Source identity and bounded collection summaries."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditColumns, Base, nonblank


class DataSource(AuditColumns, Base):
    __tablename__ = "data_sources"
    __table_args__ = (*nonblank("source_code", "display_name", "provider_name"),)

    source_code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    provider_name: Mapped[str] = mapped_column(Text)
    reference_url: Mapped[str | None] = mapped_column(Text)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        *nonblank("target_key", "processor_version"),
        CheckConstraint("status IN ('running','success','partial','failed')", name="status"),
        CheckConstraint(
            "run_kind IN ('collection','import','backfill','reprocess','legacy')", name="run_kind"
        ),
        CheckConstraint("completeness IN ('complete','partial','unknown')", name="completeness"),
        CheckConstraint("finished_at >= started_at", name="time_order"),
        CheckConstraint(
            "received >= 0 AND accepted >= 0 AND rejected >= 0 AND duplicates >= 0",
            name="counts_nonnegative",
        ),
        Index(
            "ix_collection_runs_source_target_started",
            "source",
            "target_key",
            text("started_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT")
    )
    target_key: Mapped[str] = mapped_column(Text)
    run_kind: Mapped[str] = mapped_column(Text, server_default="collection")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    completeness: Mapped[str] = mapped_column(Text, server_default="unknown")
    received: Mapped[int] = mapped_column(Integer, server_default="0")
    accepted: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    duplicates: Mapped[int] = mapped_column(Integer, server_default="0")
    processor_version: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)


class CollectionRejectionReason(Base):
    __tablename__ = "collection_rejection_reasons"
    __table_args__ = (
        *nonblank("reason_code"),
        CheckConstraint("occurrence_count > 0", name="positive_count"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    reason_code: Mapped[str] = mapped_column(Text, primary_key=True)
    occurrence_count: Mapped[int] = mapped_column(Integer)
