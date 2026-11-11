"""Short-lived API state shared by every API process (cursors, replays, trip progress).

Rows are disposable: each has an expiry and can be deleted without data loss.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class QueryCursor(Base):
    __tablename__ = "query_cursors"
    __table_args__ = (CheckConstraint("char_length(token) = 48", name="token_length"),)

    token: Mapped[str] = mapped_column(String(48), primary_key=True)
    scope = mapped_column(JSONB, nullable=False)
    payload = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TripProgressRecord(Base):
    """One row per trip: published search state plus the lease that serializes requests."""

    __tablename__ = "trip_progress"
    __table_args__ = (
        CheckConstraint("(lock_owner IS NULL) = (lock_until IS NULL)", name="lock_pair"),
    )

    trip_id: Mapped[UUID] = mapped_column(primary_key=True)
    # NULL until the first request finishes; an expired trip keeps a small tombstone.
    state = mapped_column(JSONB, nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, server_default="false")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lock_owner: Mapped[UUID | None] = mapped_column()
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TripRequestRecord(Base):
    """Cached outcome per request ID, so a retry returns the original answer."""

    __tablename__ = "trip_requests"

    trip_id: Mapped[UUID] = mapped_column(
        ForeignKey("trip_progress.trip_id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(String(64))
    outcome = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OriginContextRecord(Base):
    """Origin confirmation drafts; at most one is the trip's latest."""

    __tablename__ = "origin_contexts"
    __table_args__ = (
        Index(
            "uq_origin_contexts_latest",
            "trip_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[UUID] = mapped_column()
    draft = mapped_column(JSONB, nullable=False)
    used_by: Mapped[UUID | None] = mapped_column()
    is_latest: Mapped[bool] = mapped_column(Boolean, server_default="false")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ReadReplayEntry(Base):
    __tablename__ = "read_replays"
    __table_args__ = (CheckConstraint("btrim(replay_key) <> ''", name="replay_key_nonblank"),)

    replay_key: Mapped[str] = mapped_column(Text, primary_key=True)
    signature: Mapped[str] = mapped_column(String(64))
    outcome = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
