"""Full external-provider snapshots, including records without coordinates."""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProviderSnapshot(Base):
    __tablename__ = "provider_snapshots"
    source: Mapped[str] = mapped_column(String(160), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
