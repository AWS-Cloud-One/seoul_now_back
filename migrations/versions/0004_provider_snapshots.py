"""Preserve complete external-provider responses."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_snapshots",
        sa.Column("source", sa.String(160), primary_key=True),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("provider_snapshots")
