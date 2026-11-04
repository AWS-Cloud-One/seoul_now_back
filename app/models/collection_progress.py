"""Current scan progress only; response contents and completed scan history are not stored."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, nonblank


class CollectionCursor(Base):
    __tablename__ = "collection_cursors"
    __table_args__ = (
        UniqueConstraint("source", "target_key", "scan_id"),
        *nonblank("target_key"),
        CheckConstraint("next_page >= 1", name="next_page"),
        CheckConstraint("page_size BETWEEN 1 AND 1000", name="page_size"),
        CheckConstraint("provider_total >= 0 AND pending_total >= 0", name="totals"),
        CheckConstraint("pending_page >= 1", name="pending_page"),
        CheckConstraint("pending_digest ~ '^[0-9a-f]{64}$'", name="pending_digest"),
        CheckConstraint(
            "(pending_page IS NULL AND pending_digest IS NULL AND pending_total IS NULL) "
            "OR (pending_page IS NOT NULL AND pending_digest IS NOT NULL)",
            name="pending_end",
        ),
    )
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT"), primary_key=True
    )
    target_key: Mapped[str] = mapped_column(Text, primary_key=True)
    scan_id: Mapped[UUID] = mapped_column()
    next_page: Mapped[int] = mapped_column(Integer)
    page_size: Mapped[int] = mapped_column(Integer)
    provider_total: Mapped[int | None] = mapped_column(Integer)
    consistent_total: Mapped[bool] = mapped_column(Boolean)
    pending_page: Mapped[int | None] = mapped_column(Integer)
    pending_digest: Mapped[str | None] = mapped_column(Text)
    pending_total: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CollectionCursorPage(Base):
    __tablename__ = "collection_cursor_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source", "target_key", "scan_id"],
            [
                "collection_cursors.source",
                "collection_cursors.target_key",
                "collection_cursors.scan_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("page >= 1", name="page"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    target_key: Mapped[str] = mapped_column(Text, primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[UUID] = mapped_column()
    digest: Mapped[str] = mapped_column(Text)
