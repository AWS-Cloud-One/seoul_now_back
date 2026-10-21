"""Parking source records and collection run history."""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("rejection_reasons", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_collection_runs_source", "collection_runs", ["source"])
    op.create_table(
        "parking_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text()),
        sa.Column(
            "location",
            geoalchemy2.Geography("POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_parking_records_source", "parking_records", ["source"])
    op.create_index(
        "ix_parking_records_location", "parking_records", ["location"], postgresql_using="gist"
    )


def downgrade() -> None:
    op.drop_table("parking_records")
    op.drop_table("collection_runs")
