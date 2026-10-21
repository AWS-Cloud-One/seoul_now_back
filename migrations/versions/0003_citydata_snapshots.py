"""Persist complete latest OA-21285 snapshots per hotspot."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_runs", sa.Column("area_code", sa.String(80), nullable=True))
    op.add_column("collection_runs", sa.Column("area_name", sa.String(160), nullable=True))
    op.create_index("ix_collection_runs_area_code", "collection_runs", ["area_code"])
    op.create_table(
        "citydata_snapshots",
        sa.Column("area_code", sa.String(80), primary_key=True),
        sa.Column("area_name", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_citydata_snapshots_area_name", "citydata_snapshots", ["area_name"])
    op.create_index("ix_citydata_snapshots_collected_at", "citydata_snapshots", ["collected_at"])


def downgrade() -> None:
    op.drop_table("citydata_snapshots")
    op.drop_index("ix_collection_runs_area_code", table_name="collection_runs")
    op.drop_column("collection_runs", "area_name")
    op.drop_column("collection_runs", "area_code")
