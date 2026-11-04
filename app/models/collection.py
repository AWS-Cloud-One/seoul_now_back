"""Source identity, collection attempts and immutable input evidence."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
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


class RawPayload(Base):
    __tablename__ = "raw_payloads"
    __table_args__ = (
        UniqueConstraint("run_id", "part_key"),
        UniqueConstraint("legacy_table", "legacy_key"),
        *nonblank("part_key", "format", "file_ref", "legacy_table", "legacy_key"),
        CheckConstraint(
            "payload_kind IN "
            "('provider_response','source_file','derived_snapshot','processing_report','legacy_snapshot','legacy_observation')",
            name="kind",
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("byte_size >= 0", name="byte_size"),
        CheckConstraint(
            "document_type IN ('object','array','scalar','text','binary')", name="document_type"
        ),
        CheckConstraint(
            "(legacy_table IS NULL) = (legacy_key IS NULL)", name="legacy_identity_pair"
        ),
        CheckConstraint(
            "(legacy_collected_at IS NULL) = (legacy_time_basis IS NULL)", name="legacy_time_pair"
        ),
        CheckConstraint(
            "legacy_time_basis IN ('run_started','snapshot_saved','unknown')",
            name="legacy_time_basis",
        ),
        CheckConstraint("parent_payload_id <> payload_id", name="not_own_parent"),
    )

    payload_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str | None] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT"), index=True
    )
    part_key: Mapped[str] = mapped_column(Text)
    payload_kind: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text)
    file_ref: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    document_type: Mapped[str] = mapped_column(Text)
    parent_payload_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT"), index=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_version: Mapped[str | None] = mapped_column(Text)
    reference_date: Mapped[date | None] = mapped_column(Date)
    legacy_table: Mapped[str | None] = mapped_column(Text)
    legacy_key: Mapped[str | None] = mapped_column(Text)
    legacy_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_time_basis: Mapped[str | None] = mapped_column(Text)


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


class PayloadFileDeletion(Base):
    """Durable file cleanup after metadata deletion commits; empty after successful cleanup."""

    __tablename__ = "payload_file_deletions"
    file_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LegacyArchive(Base):
    __tablename__ = "legacy_archives"
    __table_args__ = (
        *nonblank("schema_name", "table_name", "file_ref", "source_revision"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("byte_size >= 0 AND row_count >= 0", name="counts"),
    )

    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_revision: Mapped[str] = mapped_column(Text)
    file_ref: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    row_count: Mapped[int] = mapped_column(BigInteger)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
